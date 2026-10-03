TOOL CARD: security_reference_search

Looks up general offensive-security reference knowledge — GTFOBins/LOLBAS binary-abuse
techniques and PayloadsAllTheThings payload/technique writeups — from a local, pre-embedded
corpus. Not the internet (no live fetch, no network call) and not this engagement's own history
(that's technique_recall). Use it to check exact tool syntax or a known technique for a binary
or vulnerability class you've already identified — e.g. `security_reference_search(query="find
command sudo privilege escalation")` — rather than reciting syntax from memory, which is exactly
the kind of unverified claim reporting.md warns against (the CVE-hallucination lesson applies to
tool syntax too: if you're not sure, look it up instead of guessing). A result with a low score
is a weak match, not a confirmed technique for your target — the binary/context still has to
actually be present and reachable before you act on it.
