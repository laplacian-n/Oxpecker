TOOL CARD: browser_fetch

Renders a single in-scope URL with full JavaScript execution and returns the post-render HTML —
use this only when http_recon's plain HTTP fetch won't show what you need (a client-rendered
SPA, content that only appears after JS runs), since it's slower and a materially heavier
capability than a bare HTTP GET. Every subresource the page tries to load is still validated
against the current RoE — an out-of-scope resource is blocked, not fetched, and shows up in
`denied_requests`. Denied unless the current RoE explicitly allows the browser_recon action
class.
