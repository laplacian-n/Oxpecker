TOOL CARD: run_command

No shell — argv runs directly, so pipes/redirects/semicolons/globs are literal characters, not
shell syntax; a command that depends on shell interpretation will not behave the way it would
in a terminal. Network and privilege-escalation binaries are blocked outright, not just
discouraged. Anything outside the small safe allowlist pauses for human confirmation — this is
expected friction, not a malfunction to work around by rephrasing the same command. Prefer this
tool for local, in-workspace work; it is not the path to reach a target over the network — use
the dedicated recon tools for that.
