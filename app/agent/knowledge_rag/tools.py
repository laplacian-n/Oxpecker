"""Model-callable tool for the general-knowledge RAG. IN-PROCESS, like the notebook/graph tools
— a reference lookup touches no target and needs no broker mediation. Distinct from
`technique_recall` (the agent's own past-engagement discoveries) and `knowledge_search`
(live web search): this is a static, offline, pre-embedded corpus of general offensive-security
reference material (GTFOBins/LOLBAS/PayloadsAllTheThings).
"""
from __future__ import annotations

from .service import KnowledgeRAGService

KNOWLEDGE_RAG_TOOL_NAMES = {"security_reference_search"}

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "security_reference_search",
            "description": (
                "Look up general offensive-security reference knowledge — GTFOBins/LOLBAS "
                "binary abuse techniques, PayloadsAllTheThings payload/technique writeups, "
                "HackTricks pentesting methodology (web attacks, privilege escalation, network "
                "services, exploitation), Atomic Red Team attack simulations (MITRE ATT&CK mapped), "
                "WADComs (Windows/AD commands), HijackLibs (DLL hijacking), LOLDrivers (vulnerable "
                "drivers), plus curated attack chain templates, exploit code snippets, and tool "
                "cheatsheets (nmap, sqlmap, hydra, hashcat, metasploit, impacket, etc.). "
                "Use this when you need exact tool syntax, a known technique for a binary or "
                "vulnerability class, or a step-by-step attack chain for a scenario you've "
                "identified, instead of guessing from memory (which risks a fabricated flag or "
                "CVE — see reporting.md). "
                "Not for what YOU discovered on this engagement (use technique_recall for that) "
                "and not for live internet content (use knowledge_search for that)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "what you need, e.g. 'find command privilege escalation sudo'"},
                    "source": {"type": "string", "enum": [
                                   "gtfobins", "lolbas", "payloads_all_the_things", "hacktricks",
                                   "atomic_red_team", "wadcoms", "hijacklibs", "loldrivers",
                                   "exploit_code", "cheatsheet", "sft_knowledge"],
                               "description": "restrict to one reference set, if you already know which applies"},
                },
                "required": ["query"],
            },
        },
    },
]


def dispatch(service: KnowledgeRAGService, tool_name: str, args: dict) -> dict:
    if tool_name != "security_reference_search":
        return {"ok": False, "error": f"unknown knowledge_rag tool {tool_name!r}"}
    return service.search(args.get("query", ""), source=args.get("source"))
