TOOL CARD: http_recon

Read-only — issues GET requests only, never a mechanism for exploitation attempts. Use it to
observe headers, status, redirect chain, and a capped body excerpt from a single in-scope URL.
Every redirect hop is independently scope-checked; an off-scope redirect target is reported as
a denial, not followed. `verify_cert=False` is a separate, harder-gated action — do not set it
unless explicitly instructed to, and expect it to require approval even then. A response body
being truncated is normal for large pages; say so rather than treating the excerpt as the whole
page.
