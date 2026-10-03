TOOL CARD: port_discovery

Connects to a bounded list of ports on a single in-scope host and reports open/closed/filtered
— it does not fingerprint services or attempt banners beyond what a bare TCP connect reveals.
A port list this large risks the per-call port cap; split a wide sweep into multiple calls
rather than assuming one call covers everything. "Closed" and "filtered" are different outcomes
worth distinguishing in what you report, not both just "not open."
