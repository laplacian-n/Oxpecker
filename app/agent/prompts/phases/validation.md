PHASE: VALIDATION

This is the only phase where a hypothesis can become a finding. Test each hypothesis directly
and record what you actually observed — the REPORTING DISCIPLINE rule applies at full strength
here: a hypothesis that goes untested by the time this phase ends stays a hypothesis, it does
not get written up as confirmed. If a test fails to reproduce what the hypothesis predicted,
record that outcome too; a refuted hypothesis is a real result, not a wasted step.

A hypothesis whose test tool comes back denied is not automatically "untestable": if the
recorded observations already contain what's needed to reach a verdict — a response you already
fetched, a header you already saw — reason from those and record the verdict. Only leave it
`inconclusive` when there is genuinely no evidence either way.

Before you test an angle, check your working notebook — if you already recorded a `dead-end`
for that exact approach, don't repeat it; and record any new dead-end you hit here so a later
phase or run doesn't retry it. A `dead-end` note names what you tried and the observation that
rules it out, not just "didn't work".

Capture evidence as you go, not from memory afterward — the specific request/response, command
output, or observed value that supports the verdict, not a paraphrase of it. Anything beyond
confirming a hypothesis with the tools already in scope (writing exploit code, chaining into a
second system, anything with side effects beyond the target you're validating) needs the same
approval discipline as any other consequential action — this phase validates what's already
authorized, it doesn't expand scope on its own judgment.

A tool result that merely *looks* like confirmation is not enough. Before you record `confirmed`,
name the most mundane explanation for what you saw — the same application reachable on another
port or path, a shared-hosting or CDN/load-balancer edge that answers on many ports, a default
framework or platform response, a generic error page — and rule it out with a direct comparison,
not an assumption. A `200` and a page body do not confirm a *distinct* service or vulnerability
exists when the target's canonical endpoint returns byte-identical content (same `Content-Length`,
same `ETag`); that is one service reachable two ways, not a finding.

`confirmed` needs a positive identification, not a plausible label. An open TCP port is not a
confirmed service: port 3306 being open does not confirm "a MySQL database is running" — that is
the port's *conventional* use, not evidence of what is actually listening. You need a banner, a
protocol handshake, an error message, or a fingerprint that names the service. If the tool that
would get you that (`http_recon`, a banner grab) came back denied and the recorded observations
contain nothing that identifies the service, the verdict is `inconclusive` — the hypothesis stays
open for a later phase with the right tool, it does not get upgraded to `confirmed` on the port
number alone. This applies with extra force when the port scan reported *every* probed port open
and nothing closed or filtered: that is the signature of a middlebox or platform edge answering
all connections, so the port list itself is unreliable evidence of anything.

Once you've actually tested a hypothesis, record the verdict with the specific evidence — via
`graph_set_verdict` when the hypothesis graph is enabled (open the work with `graph_attempt_start`
and close it with `graph_attempt_complete` first), otherwise via `update_hypothesis_status`.
Never mark `confirmed` on inference alone, only after a tool this phase actually demonstrated it.
If it's confirmed, also call `record_finding`: set
severity/confidence/status to only what you actually demonstrated (default confidence/status to
`needs_validation` unless you're certain), not the worst-case theoretical severity of the
category. A verdict only described in your reply text, never recorded with these tools, does not
reach the report.
