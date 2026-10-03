TOOL CARD: knowledge_fetch

Fetches a single external URL outside the RoE-scoped target — for reading a page found via
knowledge_search, not for probing the target itself. Blocked outright for loopback/private/
link-local/cloud-metadata addresses regardless of RoE (SSRF protection); denied entirely unless
the current RoE explicitly allows the knowledge_fetch action class. Single hop only — a redirect
is reported, not followed; call this again with the new URL if you want to follow it.
