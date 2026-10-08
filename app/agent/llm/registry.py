"""Name -> provider, in one place.

Two reasons this is a module rather than a dict at a call site. A session has to record WHICH
provider served it, so the set of legal names has to be enumerable rather than implied by
whatever happened to be imported. And an unknown name has to fail at session creation with the
list of real ones, not at the first turn — the runtime already learned that lesson with
`isolation_tier`, where an unvalidated name reached `get_executor()` and raised mid-turn on a
session that had been reporting that tier since it was created.
"""
from __future__ import annotations

from .base import ChatProvider, ProviderCapabilities

PROVIDER_LLAMA_CPP = "llama.cpp"

#: Every provider this build can serve. A name absent here cannot be selected.
PROVIDERS = (PROVIDER_LLAMA_CPP,)

DEFAULT_PROVIDER = PROVIDER_LLAMA_CPP


class UnknownProviderError(ValueError):
    pass


def capabilities_for(name: str) -> ProviderCapabilities:
    """What `name` offers, without constructing it.

    Separate from `build` on purpose: the UI and the API want to show what a provider can do
    before a session exists, and constructing the llama.cpp provider connects to a server.
    """
    if name == PROVIDER_LLAMA_CPP:
        from .llama import LlamaCppProvider

        return LlamaCppProvider.CAPABILITIES
    raise UnknownProviderError(
        f"unknown model provider {name!r}; choose from {list(PROVIDERS)}"
    )


def build(name: str = DEFAULT_PROVIDER, **kwargs) -> ChatProvider:
    """Construct the provider. Raises UnknownProviderError rather than returning a default — a
    silent fallback to the local model would mean a session reporting one provider and running
    another, which is the class of defect this package exists to avoid."""
    if name == PROVIDER_LLAMA_CPP:
        from .llama import LlamaCppProvider

        return LlamaCppProvider(**kwargs)
    raise UnknownProviderError(
        f"unknown model provider {name!r}; choose from {list(PROVIDERS)}"
    )
