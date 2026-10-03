TOOL CARD: osint_record

For filing something you noticed incidentally while working in-scope — a subdomain mentioned in
a page, an email address in response content, a linked third-party service. Never use this to
then go probe or fetch the thing you recorded; it has no fetch capability itself and using
another tool against an out-of-scope target you just filed here would still be a scope
violation. Returns a clear failure (not a fabricated success) if the current engagement has no
structured state store to record into.
