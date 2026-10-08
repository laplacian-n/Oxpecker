"""What the runtime needs from a model provider, and what a provider must declare about itself.

Oxpecker drives a local llama.cpp server. API mode (docs/API_MODE_DESIGN.md) drives a frontier
model over an external API instead, for engagements where capability matters more than running
offline, and where the trajectories produced are meant to train the local model afterwards.

The decision that shapes this package is in that document: **do not fork the runtime.** This
project already paid for a fork once — the finding in docs/OBSERVABILITY_PLAN.md §1 was that the
shipped runtime used none of the four safety controls the docs claimed, because two runtimes
drifted with nobody diffing them. So the provider moves behind an interface and everything
downstream — broker, audit chain, evidence store, tool surface — stays shared.

This module is deliberately only the contract. It imports nothing from the project but the
standard library, so a provider implementation can import it without pulling in a server.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# How faithfully a provider returns the model's reasoning. The dataset goal makes this the most
# consequential field in the whole package, so it is a declared capability rather than something
# a later reader infers:
#
#   "raw"     — the actual token stream the model produced. Only a locally-hosted model can
#               offer this, and it is the only form that is reasoning-trace training data.
#   "summary" — a readable summary of the reasoning, not the reasoning. A different distribution
#               from what the model emitted; training on it teaches a model to write summaries
#               of thinking rather than to think.
#   "omitted" — reasoning happened and is billed, but nothing is returned.
#   "none"    — the provider has no reasoning channel at all.
#
# Current frontier APIs never return a raw chain of thought. That is not an argument against
# API mode; it reframes what API mode is FOR — trajectories and outcome labels, not reasoning.
REASONING_FIDELITIES = ("raw", "summary", "omitted", "none")


@dataclass(frozen=True)
class ProviderCapabilities:
    """What this provider can do. The pipeline branches on these, and every turn records them,
    so a trajectory says which path produced it rather than leaving a later reader to guess.

    Defaults describe the weakest plausible provider, so a new implementation that forgets to
    declare something is treated as not having it rather than as having it.
    """

    name: str
    model: str = ""

    # Structured tool_use blocks, versus recovering a tool call out of prose. dev_server carries
    # an alias table and a JSON-block scraper precisely because a 4B model needs them; a
    # provider that emits real tool calls should not be put through that, and the trajectory
    # must record which path parsed the call — "the model emitted a valid call" and "we
    # recovered one from prose" are different facts about the model.
    native_tool_calls: bool = False
    strict_tool_schemas: bool = False

    prompt_caching: bool = False
    server_compaction: bool = False
    server_token_count: bool = False

    # How much the model will actually accept, in tokens. The runtime's context budget was a
    # hardcoded character count sized for one local llama-server build, which is wrong in both
    # directions once a second provider exists: it compacts a 200k-context API model after 22k
    # tokens, and it would overrun a model with a smaller window than the one it was tuned for.
    # The default is the smallest window any provider here plausibly has, so a provider that
    # does not declare one is budgeted conservatively rather than optimistically.
    context_window: int = 32768

    reasoning: str = "none"
    # Whether editing earlier turns invalidates prior reasoning blocks. True means the harness
    # must be append-only: the runtime's _fit_context and _maybe_compact both rewrite history,
    # which is correct for llama.cpp and rejected by providers that bind reasoning to the
    # conversation that produced it.
    append_only_history: bool = False

    # (input, output) USD per million tokens, when the provider charges. None for a local model,
    # which is not free but is not metered per token either.
    cost_per_mtok: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.reasoning not in REASONING_FIDELITIES:
            raise ValueError(
                f"reasoning={self.reasoning!r} is not one of {list(REASONING_FIDELITIES)}"
            )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "native_tool_calls": self.native_tool_calls,
            "strict_tool_schemas": self.strict_tool_schemas,
            "prompt_caching": self.prompt_caching,
            "server_compaction": self.server_compaction,
            "server_token_count": self.server_token_count,
            "context_window": self.context_window,
            "reasoning": self.reasoning,
            "append_only_history": self.append_only_history,
            "cost_per_mtok": list(self.cost_per_mtok) if self.cost_per_mtok else None,
        }


@runtime_checkable
class ChatProvider(Protocol):
    """The shape the web runtime already calls, named so a second implementation can satisfy it.

    `chat` is deliberately typed loosely (`Any`): with `stream=False` it returns the provider's
    response dict, and with `stream=True` a generator of chunks. That is the existing contract,
    and narrowing it here would mean changing every call site in the same commit as introducing
    the seam — two risks for the price of one.
    """

    def capabilities(self) -> ProviderCapabilities: ...

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 3072,
        temperature: float = 0.6,
        stream: bool = False,
        tools: list[dict] | None = None,
        enable_thinking: bool = False,
    ) -> Any: ...

    def token_count(self, text: str) -> int: ...
