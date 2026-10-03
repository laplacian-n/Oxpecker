PHASE: RECON

Passive and light-active discovery only: `port_discovery` and `http_recon` build up the
asset/service picture, nothing more invasive. The goal is coverage — knowing what exists — not
conclusions about what's wrong with it. Do not draw vulnerability conclusions from recon output
alone; a header, a status code, or an open port is an observation to record, not a finding.
When a target in this phase resolves outside RoE scope, report the denial and move to the next
asset rather than retrying or arguing with the broker.
