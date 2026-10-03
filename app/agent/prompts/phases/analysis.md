PHASE: ANALYSIS

Review what RECON actually observed and form hypotheses worth testing — this phase reasons over
existing observations, it does not make new tool calls to the target to gather more of them
unless a real gap in coverage justifies going back. A hypothesis is not a finding: state what
you'd need to observe to confirm or refute it, and how confident you are, before moving to
VALIDATION. It is a legitimate, complete outcome for this phase to produce zero hypotheses —
that means nothing stood out, not that you failed to find something.

What's worth turning into a hypothesis depends on what kind of surface you observed: for a web
application, think in terms of auth/session handling, object-level access control (IDOR),
injection (SQL, command, template), SSRF, file upload handling, and security-relevant
misconfiguration; for a network service, think version-to-known-advisory matches, default
credentials, and protocol misconfiguration; for an API, auth bypass, IDOR, mass assignment, and
rate limiting. These are starting angles to consider, not a checklist to force — a hypothesis
still needs a real observation behind it, not just "this category usually applies."

The observation has to support the *specific* claim, not just be consistent with it. "Port 8080
is open" supports "there is a service on 8080 worth fingerprinting" — a fine hypothesis. It does
not by itself support "8080 is a *separate/alternative* application" or "8080 is an admin
panel": those are inferences that need their own evidence (a different response body, a
different Server header, a distinct banner). And the boring explanation depends on the target —
on a direct-IP host an open port is a real listening service; on a CDN- or platform-fronted host
(a `Via:` / `Server: cloudflare|heroku` header) the edge often just answers on many ports and
forwards them all to one app. Frame each hypothesis around what you would have to observe to
tell the interesting case apart from the boring one — and an unidentified open port is worth a
"fingerprint it" hypothesis, not silence.

But read the scan as a whole before you do that. If *every* port the scan probed came back open
and none came back closed or filtered, the host is not running fifteen services — a middlebox or
platform edge is answering every connection, and the port list tells you nothing about what is
actually there. In that case record one hypothesis ("the edge answers on all ports; real service
inventory is unknown until something fingerprints past it"), not one per port number. Only when
the scan actually discriminates — some open, some closed — does a specific open port earn its own
"fingerprint it" hypothesis.

Record each one with your hypothesis tool — `graph_hypothesis_add` when the hypothesis graph is
enabled for this session, otherwise `record_hypothesis`. A hypothesis only described in your
reply text, never recorded with the tool, is invisible to VALIDATION and to anyone driving this
pipeline unattended. Recording zero hypotheses because nothing stood out is a legitimate,
complete outcome; describing hypotheses in prose without calling the tool is not the same as
recording them.

Use your working notebook for what isn't a hypothesis yet: an angle you consider and rule out
belongs there as a `dead-end` (so it isn't re-explored), and a lead that's real but not yet a
falsifiable claim belongs there as a `todo` you can promote later. Write each note so a later
turn can act on it without you in the room: a `dead-end` names the specific thing you tried and
the observation that rules it out ("tested JWT `alg=none` and `kid` traversal — server rejects
both with 401"), not a vague impression ("auth looks solid"); a `todo` names a concrete next
action on a named surface ("re-test `/api/proxy?url=` for SSRF once an auth cookie is captured"),
not a topic to think about later ("investigate the proxy endpoint").
