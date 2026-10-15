"""A persistent, per-engagement browser that the operator and the agent share.

`service.browser_fetch` is one-shot by design: a fresh ephemeral context per call, torn down at
the end, never reused. That is the right shape for "render this page and tell me what it says",
and its guarantee is asserted by test_service.py. It is the wrong shape for the thing most real
web findings need — IDOR, broken access control, business-logic flaws all live behind a login,
and a context that dies after every call can never hold one.

So this is a SECOND path, not a change to the first. Its profile persists on disk per engagement,
which is exactly what lets the operator sign in once and hand the agent that session.

What is deliberately identical to the one-shot path:

  * **The egress gate.** `service._make_route_handler` is reused unchanged, so every request the
    page makes — navigation, script, image, XHR, iframe — is validated against the same
    `scope_check.validate_target` every other tool uses and aborted before it leaves the process.
    One gate, not two: a second implementation of the same policy is a divergence waiting to
    happen, and this one guards the scope check, which no tier may remove.

What is deliberately different, and must be understood before using it:

  * **The profile is the engagement-shared vector of AGENT_ARCHITECTURE §14.1 C.** Cookies,
    storage and cache survive across calls and across workers in the same engagement. That is the
    point, and it is also why a flagged result from this path must mark taint as `shared=True`:
    attacker-controlled material reaching this profile reaches every worker on the engagement,
    not just the session that fetched it. taint.py's own docstring names the browser profile as
    this vector.
  * **Engagements never share one.** The profile lives under the engagement's own directory, so
    one engagement's logged-in session cannot be reached from another.

### Why a thread per engagement

Playwright's sync API binds its objects to the thread that created them; driving a page from a
different thread raises `cannot switch to a different thread`, which was measured rather than
assumed. A persistent browser outlives any one request, and the web runtime serves requests on
many threads, so the objects get a thread of their own and callers post work to it. Everything
crossing that boundary is a plain callable returning plain data.
"""
from __future__ import annotations

import base64
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from ..broker.policy import Policy
from .service import _make_route_handler, _launch_kwargs

log = logging.getLogger("agent.browser.live")

# How long a caller waits for the browser thread to finish one command. A hung page must not hold
# a web worker forever; the caller gets a timeout and the browser stays usable for the next one.
COMMAND_TIMEOUT_S = 60.0
# How long stop() waits for the thread to tear its own objects down, before giving up on a clean
# close. The thread is a daemon, so a stuck browser cannot keep the process alive either way.
SHUTDOWN_TIMEOUT_S = 20.0
# How often the browser thread pumps Playwright while a screencast is running.
#
# Measured, because it decides whether the live view works at all: sync Playwright dispatches
# events only while the owning thread is INSIDE a Playwright call. A thread parked on its command
# queue receives nothing — 0 frames in 3 seconds against a page repainting 5 times a second. The
# same 3 seconds with a cheap call every 100ms delivered 18 frames. So while anyone is watching,
# the loop wakes on this interval and makes one trivial round-trip to let the frames through.
# Nobody watching means no screencast, no timeout and no cost: the thread parks as before.
PUMP_INTERVAL_S = 0.1

# The key for the operator's own page in the per-worker page map. The operator watches ONE page —
# their own — not whichever worker happened to navigate last, so the screencast and `/browser/input`
# bind to this key, and an agent worker never gets it (worker ids are opaque engagement-scoped
# strings, never this sentinel).
OPERATOR_PAGE = "__operator__"


class BrowserUnavailableError(RuntimeError):
    """The browser could not be started — no Chromium, or the profile could not be opened."""


class _Command:
    __slots__ = ("fn", "worker", "resolve", "result", "error", "done")

    def __init__(self, fn: Callable[[Any], Any], *, worker: str | None = None,
                 resolve: bool = True):
        self.fn = fn
        # Which page this command runs against: a worker id, or OPERATOR_PAGE for the operator's.
        self.worker = worker
        # True: the thread resolves the page and calls fn(page). False: a page-map operation that
        # needs the browser itself, so the thread calls fn(self) instead (see close_page).
        self.resolve = resolve
        self.result: Any = None
        self.error: BaseException | None = None
        self.done = threading.Event()


class LiveBrowser:
    """One persistent browser for one engagement. Thread-safe; every caller's work runs on the
    browser's own thread, in submission order.

    One BrowserContext per engagement (the login-holding, scope-gated profile) holds MANY pages:
    the operator's one page, plus one page per agent worker, created on first use. Chromium
    separates the cookie jar (on the context) from navigation (on the page), which is exactly the
    split the engagement needs — the operator signs in once and every worker's page shares that
    session, while worker A navigating to `/admin` does not drag worker B's page off `/profile`.

    The egress gate rides on `context.route`, not on the page, so a page created later for a new
    worker inherits the same scope check automatically — there is no per-page wiring step that
    could be forgotten, which is why page-per-worker is safe by construction rather than by care.
    """

    def __init__(self, engagement_id: str, profile_dir: Path, policy: Policy, *, headless: bool = True):
        self.engagement_id = engagement_id
        self.profile_dir = Path(profile_dir)
        self.policy = policy
        self.headless = headless
        self.denied_requests: list[dict] = []
        # worker id (or OPERATOR_PAGE) -> Page. Only ever touched on the browser thread, so no
        # lock: pages are both created and closed inside commands dispatched by _run.
        self._pages: dict[str, Any] = {}
        self._context = None  # set on the browser thread once the context is up
        # The live view's single slot. Only ever the newest frame: a viewer wants what the page
        # looks like NOW, and a queue of stale frames is latency pretending to be history.
        self._frame: bytes | None = None
        self._frame_seq = 0
        self._frame_meta: dict = {}
        self._frame_lock = threading.Lock()
        # Set on the browser thread only, read there to decide whether to pump.
        self._screencast = None
        self._queue: queue.Queue[_Command | None] = queue.Queue()
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name=f"live-browser-{engagement_id}", daemon=True
        )
        self._thread.start()
        self._started.wait(timeout=COMMAND_TIMEOUT_S)
        if self._start_error is not None:
            raise BrowserUnavailableError(
                f"{type(self._start_error).__name__}: {self._start_error}"
            ) from self._start_error
        if not self._started.is_set():
            raise BrowserUnavailableError("the browser thread did not come up in time")

    # -- the browser's own thread ----------------------------------------------------------

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        context = None
        pw = None
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            pw = sync_playwright().start()
            # launch_persistent_context, not launch()+new_context(): the profile directory IS the
            # persistence. It returns the context directly — there is no separate browser object
            # to hold, and closing the context closes the browser.
            context = pw.chromium.launch_persistent_context(
                str(self.profile_dir), **_launch_kwargs(self.headless)
            )
            context.route("**/*", _make_route_handler(self.policy, self.denied_requests))
            # The operator's page: the one Chromium already opened, or a fresh one. Worker pages are
            # created lazily by _resolve_page the first time a worker drives the browser.
            operator_page = context.pages[0] if context.pages else context.new_page()
            self._context = context
            self._pages[OPERATOR_PAGE] = operator_page
        except BaseException as e:  # noqa: BLE001 - reported to the constructor, not swallowed
            self._start_error = e
            self._started.set()
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass
            return

        self._started.set()
        try:
            while True:
                try:
                    # A timeout only while a screencast is running; see PUMP_INTERVAL_S. Without
                    # one the thread blocks here and no frame is ever dispatched.
                    cmd = self._queue.get(
                        timeout=PUMP_INTERVAL_S if self._screencast is not None else None
                    )
                except queue.Empty:
                    try:
                        # The pump flushes screencast events, which come off the OPERATOR page —
                        # that is the only page the operator watches.
                        self._pages[OPERATOR_PAGE].title()  # one cheap round-trip
                    except Exception:  # noqa: BLE001 - a dead page must not kill the thread
                        pass
                    continue
                if cmd is None:  # stop() sentinel
                    return
                try:
                    if cmd.resolve:
                        cmd.result = cmd.fn(self._resolve_page(cmd.worker))
                    else:
                        cmd.result = cmd.fn(self)  # a page-map op; see close_page
                except BaseException as e:  # noqa: BLE001 - handed back to the caller verbatim
                    cmd.error = e
                finally:
                    cmd.done.set()
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass

    def _resolve_page(self, worker: str | None):
        """The page for `worker` (OPERATOR_PAGE when None), created lazily. Browser thread only.

        A new page is created with `context.new_page()`, which must happen on this thread, and it
        inherits the context's `route` egress gate the moment it exists — no per-page wiring.
        """
        key = worker or OPERATOR_PAGE
        page = self._pages.get(key)
        if page is None:
            page = self._context.new_page()
            self._pages[key] = page
        return page

    # -- the public, any-thread surface ----------------------------------------------------

    def call(self, fn: Callable[[Any], Any], *, worker: str | None = None,
             timeout: float = COMMAND_TIMEOUT_S) -> Any:
        """Run `fn(page)` on the browser's thread and return its result.

        `worker` selects whose page `fn` drives: an agent worker's id, or None for the operator's
        page (the one the screencast shows). Each worker gets its own page, created on first call,
        so parallel workers navigate independently while sharing the one logged-in profile.

        The callable is the whole API on purpose: anything that touches a Playwright object has
        to happen over there, so handing the page to a closure is the only boundary that cannot
        be used wrongly by accident.
        """
        if self._closed:
            raise BrowserUnavailableError("this browser has been stopped")
        cmd = _Command(fn, worker=worker)
        self._queue.put(cmd)
        if not cmd.done.wait(timeout=timeout):
            raise TimeoutError(f"the browser did not finish the command within {timeout}s")
        if cmd.error is not None:
            raise cmd.error
        return cmd.result

    def close_page(self, worker_id: str, *, timeout: float = COMMAND_TIMEOUT_S) -> None:
        """Close one worker's page when that worker is done, so pages do not accumulate for the
        life of the engagement. The operator's page cannot be closed this way — it is the one the
        human is watching — and closing a worker that never opened a page is a no-op."""
        if not worker_id or worker_id == OPERATOR_PAGE or self._closed:
            return

        def shut(lb: "LiveBrowser"):
            page = lb._pages.pop(worker_id, None)
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001 - a page already gone is the outcome we wanted
                    pass

        cmd = _Command(shut, resolve=False)
        self._queue.put(cmd)
        cmd.done.wait(timeout=timeout)

    # -- the live view ---------------------------------------------------------------------

    def start_screencast(self, *, quality: int = 60, max_width: int = 1280,
                         max_height: int = 720) -> None:
        """Begin streaming what the page looks like, into this browser's single frame slot.

        Chromium emits a frame on repaint rather than on a clock, so a still page costs nothing
        and a busy one is naturally rate-limited. Each frame must be acknowledged or Chromium
        stops sending: the ack is not optional bookkeeping, it is the flow control.
        """
        def begin(page):
            if self._screencast is not None:
                return
            cdp = page.context.new_cdp_session(page)

            def on_frame(params):
                # Runs on this same browser thread, dispatched out of the pump.
                try:
                    data = base64.b64decode(params["data"])
                except Exception:  # noqa: BLE001 - a malformed frame is skipped, not fatal
                    return
                with self._frame_lock:
                    self._frame = data
                    self._frame_seq += 1
                    self._frame_meta = dict(params.get("metadata") or {})
                try:
                    cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                except Exception:  # noqa: BLE001 - the stream ends; the viewer sees a stale frame
                    log.debug("screencast ack failed for %s", self.engagement_id, exc_info=True)

            cdp.on("Page.screencastFrame", on_frame)
            cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": quality,
                "maxWidth": max_width, "maxHeight": max_height, "everyNthFrame": 1,
            })
            self._screencast = cdp

        self.call(begin)

    def stop_screencast(self) -> None:
        """Stop streaming. The last frame stays readable, so a paused view does not go blank."""
        def end(page):  # noqa: ARG001 - the page is the handle the loop passes; unused here
            cdp, self._screencast = self._screencast, None
            if cdp is None:
                return
            try:
                cdp.send("Page.stopScreencast")
            except Exception:  # noqa: BLE001 - already gone is the outcome we wanted
                pass

        self.call(end)

    @property
    def screencasting(self) -> bool:
        return self._screencast is not None

    def latest_frame(self) -> tuple[bytes, int, dict] | None:
        """The newest frame as (jpeg_bytes, sequence, metadata), or None before the first one.

        The sequence lets a viewer tell a fresh frame from the one it already has without
        comparing the bytes, which is what makes polling cheap enough to be smooth.
        """
        with self._frame_lock:
            if self._frame is None:
                return None
            return self._frame, self._frame_seq, dict(self._frame_meta)

    def stop(self) -> None:
        """Close the browser and release the profile. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=SHUTDOWN_TIMEOUT_S)

    @property
    def alive(self) -> bool:
        return not self._closed and self._thread.is_alive()


# -- the per-engagement registry -----------------------------------------------------------

_browsers: dict[str, LiveBrowser] = {}
# Guards the whole get-or-create sequence, not just the dict lookup. Reading "is there one?" and
# then creating one are a single operation or two callers each launch a browser against the same
# profile directory — and two Chromiums on one profile is a corrupted profile, not just waste.
_registry_lock = threading.Lock()


def profile_dir_for(engagement_dir: Path) -> Path:
    return Path(engagement_dir) / "browser_profile"


def get(engagement_id: str, engagement_dir: Path, policy: Policy, *,
        headless: bool = True) -> LiveBrowser:
    """The engagement's live browser, started on first use."""
    with _registry_lock:
        existing = _browsers.get(engagement_id)
        if existing is not None and existing.alive:
            return existing
        browser = LiveBrowser(engagement_id, profile_dir_for(engagement_dir), policy,
                              headless=headless)
        _browsers[engagement_id] = browser
        return browser


def stop(engagement_id: str) -> bool:
    """Stop and forget one engagement's browser. Returns whether there was one."""
    with _registry_lock:
        browser = _browsers.pop(engagement_id, None)
    if browser is None:
        return False
    browser.stop()
    return True


def stop_all() -> None:
    with _registry_lock:
        browsers = list(_browsers.values())
        _browsers.clear()
    for b in browsers:
        b.stop()


def close_worker_page(engagement_id: str, worker_id: str) -> None:
    """Close a finished worker's page on its engagement's browser, if there is one. Best-effort:
    a worker that never drove the browser, or an engagement with no live browser, is a no-op, and
    the browser itself (with its shared profile and the operator's page) is left running."""
    with _registry_lock:
        browser = _browsers.get(engagement_id)
    if browser is not None and browser.alive:
        try:
            browser.close_page(worker_id)
        except Exception:  # noqa: BLE001 - cleanup must never fail a wave
            log.debug("close_worker_page failed for %s/%s", engagement_id, worker_id, exc_info=True)
