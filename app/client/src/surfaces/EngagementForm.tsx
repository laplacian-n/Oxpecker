import { useEffect, useRef, useState } from "react";
import { api, ApiOptions } from "../api";

// Creating an engagement from /app/. Until now the React client could only open one that already
// existed — Landing said so in as many words, pointing the operator at the classic UI — which made
// /app/ unusable as the only UI.
//
// An engagement is the authorisation record: the targets the agent may touch, who signed off, how
// long it is valid, and which classes of action are permitted. The broker enforces all four, so
// this form is not a convenience wrapper over a config file; it is where the authorisation is
// written down.
//
// Two things it deliberately does NOT do:
//
//   * validate scope entries. The rules live in agent/engagement/intake.py, which delegates to
//     the broker's own parser precisely so that what intake accepts is what the broker enforces.
//     A copy here would be a third opinion — and the second one, when it existed, rejected
//     `*.example.com`, the form every bug-bounty scope is written in. The server's message is
//     shown verbatim instead.
//   * carry its own list of action classes. Three hardcoded copies had already drifted apart, and
//     because the broker refuses on a class an engagement does not grant, a stale copy appears at
//     runtime as a tool that silently cannot run — which is how the live browser ended up
//     forbidden on the default engagement. The list comes from /api/action-classes, defaults and
//     all.

interface ActionClass {
  action_class: string;
  tools: string[];
  default: boolean;
  note: string;
}

export function EngagementForm({
  apiOptions = {},
  onCreated,
  apiImpl = api,
}: {
  apiOptions?: ApiOptions;
  onCreated: (engagementId: string) => void;
  apiImpl?: Pick<typeof api, "actionClasses" | "createEngagement">;
}) {
  const [open, setOpen] = useState(false);
  const [classes, setClasses] = useState<ActionClass[] | null>(null);
  const [granted, setGranted] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const idRef = useRef<HTMLInputElement>(null);
  const descRef = useRef<HTMLInputElement>(null);
  const allowRef = useRef<HTMLTextAreaElement>(null);
  const denyRef = useRef<HTMLTextAreaElement>(null);
  const byRef = useRef<HTMLInputElement>(null);
  const hoursRef = useRef<HTMLInputElement>(null);

  // Fetched when the form opens, not on mount: the landing's first job is to list what exists,
  // and an operator who only ever opens an engagement should not pay for this request.
  useEffect(() => {
    if (!open || classes) return;
    let live = true;
    (async () => {
      try {
        const rows = (await apiImpl.actionClasses(apiOptions)) as ActionClass[];
        if (!live) return;
        setClasses(rows);
        setGranted(new Set(rows.filter((r) => r.default).map((r) => r.action_class)));
      } catch (e: any) {
        if (!live) return;
        // Without the list there is nothing honest to pre-tick: submitting the defaults the
        // client *guessed* is how the drift this form avoids got started. Say so and stop.
        setClasses([]);
        setError(`Could not load the action classes: ${String(e?.message ?? e)}`);
      }
    })();
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, classes, apiImpl]);

  const lines = (el: HTMLTextAreaElement | null): string[] =>
    (el?.value ?? "").split("\n").map((l) => l.trim()).filter(Boolean);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const body = {
        engagement_id: (idRef.current?.value ?? "").trim(),
        description: (descRef.current?.value ?? "").trim(),
        allow_targets: lines(allowRef.current),
        deny_targets: lines(denyRef.current),
        allowed_action_classes: [...granted],
        authorized_by: (byRef.current?.value ?? "").trim(),
        // Sent only when the operator typed something: the server's own default (24h) is the one
        // that should apply otherwise, not a number this form invents.
        ...(hoursRef.current?.value ? { valid_hours: Number(hoursRef.current.value) } : {}),
      };
      const created = await apiImpl.createEngagement(body, apiOptions);
      onCreated(created?.engagement_id ?? body.engagement_id);
      setOpen(false);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} data-testid="new-engagement">New engagement</button>
    );
  }

  return (
    <form onSubmit={submit} className="engagement-form" data-testid="engagement-form">
      <label>
        Engagement id
        <input ref={idRef} data-testid="eng-id" autoComplete="off" placeholder="acme-staging" />
      </label>
      <label>
        Description <span className="hint">(optional)</span>
        <input ref={descRef} data-testid="eng-desc" autoComplete="off" />
      </label>
      <label>
        In-scope targets <span className="hint">(one per line; a host, CIDR or *.wildcard)</span>
        <textarea ref={allowRef} data-testid="eng-allow" rows={3} placeholder={"staging.acme.com\n10.0.0.0/24"} />
      </label>
      <label>
        Out-of-scope <span className="hint">(optional, one per line — wins over in-scope)</span>
        <textarea ref={denyRef} data-testid="eng-deny" rows={2} />
      </label>
      <label>
        Authorised by <span className="hint">(who signed this off)</span>
        <input ref={byRef} data-testid="eng-by" autoComplete="off" />
      </label>
      <label>
        Valid for <span className="hint">(hours; blank = the server's default)</span>
        <input ref={hoursRef} data-testid="eng-hours" type="number" min="1" step="1" />
      </label>

      <fieldset className="action-classes" data-testid="eng-classes">
        <legend>Permitted actions</legend>
        {classes === null ? (
          <p data-testid="classes-loading">Loading…</p>
        ) : (
          classes.map((c) => (
            <label key={c.action_class} className="action-class">
              <input
                type="checkbox"
                data-testid={`class-${c.action_class}`}
                checked={granted.has(c.action_class)}
                onChange={(ev) =>
                  setGranted((prev) => {
                    const next = new Set(prev);
                    if (ev.target.checked) next.add(c.action_class);
                    else next.delete(c.action_class);
                    return next;
                  })
                }
              />
              <span>
                <code>{c.action_class}</code>
                {c.note ? <span className="hint"> — {c.note}</span> : null}
                {c.tools.length ? <span className="hint"> ({c.tools.join(", ")})</span> : null}
              </span>
            </label>
          ))
        )}
      </fieldset>

      <div className="engagement-form-actions">
        <button type="submit" disabled={busy} data-testid="create-engagement">
          {busy ? "Creating…" : "Create engagement"}
        </button>
        <button type="button" onClick={() => setOpen(false)} data-testid="cancel-engagement">Cancel</button>
      </div>
      {/* The server's own words. It reports every problem it found at once, so an operator fixes
          the form in one pass instead of one field per submit. */}
      {error && <p className="landing-error" data-testid="eng-error">{error}</p>}
    </form>
  );
}
