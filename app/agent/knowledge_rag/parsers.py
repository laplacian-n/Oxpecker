"""Corpus parsers — turn the three source repos under `corpus_src/` into a flat list of
{id, source, title, text, tags, url} chunks, one embeddable unit each. GTFOBins and LOLBAS are
structured YAML (parsed field-by-field, high signal); PayloadsAllTheThings is prose markdown
(split by header boundaries, since there's no other structure to key off).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def parse_gtfobins(root: Path) -> list[dict]:
    """One chunk per (binary, function, context) — the code + comment + what the function type
    means (from _data/functions.yml's label/description), so the embedding carries real
    semantics, not just a bare command."""
    func_meta = yaml.safe_load((root / "_data" / "functions.yml").read_text())
    chunks = []
    for path in sorted((root / "_gtfobins").iterdir()):
        if not path.is_file():
            continue
        binary = path.name
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if not doc or "functions" not in doc:
            continue
        for func_name, entries in (doc.get("functions") or {}).items():
            meta = func_meta.get(func_name, {})
            label = meta.get("label", func_name)
            description = meta.get("description", "")
            for entry_idx, entry in enumerate(entries or []):
                contexts = entry.get("contexts") or {}
                base_code = entry.get("code", "")
                base_comment = entry.get("comment", "")
                if not contexts:
                    contexts = {"unspecified": None}
                for ctx_name, ctx_override in contexts.items():
                    code = base_code
                    comment = base_comment
                    if isinstance(ctx_override, dict):
                        code = ctx_override.get("code", base_code)
                        comment = ctx_override.get("comment", base_comment)
                    if not code:
                        continue
                    text = (
                        f"GTFOBins: {binary} — {label} ({ctx_name} context).\n"
                        f"{description}\n"
                        f"Command:\n{code}"
                    )
                    if comment:
                        text += f"\nNote: {comment}"
                    chunks.append({
                        "id": f"gtfobins:{binary}:{func_name}:{entry_idx}:{ctx_name}",
                        "source": "gtfobins",
                        "title": f"{binary} — {label} ({ctx_name})",
                        "text": text,
                        "tags": [binary, func_name, ctx_name, "linux"],
                        "url": f"https://gtfobins.github.io/gtfobins/{binary}/",
                    })
    return chunks


def parse_lolbas(root: Path) -> list[dict]:
    """One chunk per Command entry — LOLBAS's Windows-binary equivalent of GTFOBins.

    LOLBAS.github.io migrated its data from a flat `yml/**/*.yml` layout to Jekyll frontmatter
    inside `_lolbas/**/*.md` (same Name/Commands/Resources schema either way — confirmed by
    cloning the real repo, which has no `yml/` directory at all: this parser produced zero
    chunks against it, silently, because `test_produces_many_chunks` is the only one of the
    four lolbas tests that doesn't pass vacuously on an empty list). Both layouts are read here
    so this keeps working if a `yml/` export ever comes back.
    """
    chunks = []
    yml_dir = root / "yml"
    docs: list[tuple[dict, str]] = []
    for path in sorted(yml_dir.rglob("*.yml")):
        try:
            docs.append((yaml.safe_load(path.read_text(encoding="utf-8")), path.stem))
        except yaml.YAMLError:
            continue
    md_dir = root / "_lolbas"
    for path in sorted(md_dir.rglob("*.md")):
        raw = path.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", raw, re.DOTALL)
        if not m:
            continue
        try:
            docs.append((yaml.safe_load(m.group(1)), path.stem))
        except yaml.YAMLError:
            continue
    for doc, default_name in docs:
        if not doc or "Commands" not in doc:
            continue
        name = doc.get("Name", default_name)
        for i, cmd in enumerate(doc.get("Commands") or []):
            command = cmd.get("Command", "")
            if not command:
                continue
            category = cmd.get("Category", "")
            usecase = cmd.get("Usecase", "")
            description = cmd.get("Description", "")
            mitre = cmd.get("MitreID", "")
            text = (
                f"LOLBAS: {name} — {category} ({usecase}).\n"
                f"{description}\n"
                f"Command:\n{command}"
            )
            if mitre:
                text += f"\nMITRE ATT&CK: {mitre}"
            chunks.append({
                "id": f"lolbas:{name}:{i}",
                "source": "lolbas",
                "title": f"{name} — {category or usecase}",
                "text": text,
                "tags": [name, category, "windows"],
                "url": doc.get("Resources", [{}])[0].get("Link", "") if doc.get("Resources") else "",
            })
    return chunks


_MIN_SECTION_CHARS = 40
_SKIP_TITLES = {"summary", "table of contents", "references", "resources", "labs"}


def parse_payloads_all_the_things(root: Path) -> list[dict]:
    """Generic markdown chunker: split each file on ## / ### header boundaries, keep the header
    breadcrumb as context, skip pure-navigation sections (Summary/TOC/References) and anything
    too short to be a real technique on its own."""
    chunks = []
    skip_dirs = {".git", ".github", "images", "_template_vuln"}
    for path in sorted(root.rglob("*.md")):
        if any(part in skip_dirs for part in path.parts):
            continue
        topic = path.parent.name if path.parent != root else path.stem
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        h1 = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), path.stem)

        sections: list[tuple[str, list[str]]] = []
        current_title = h1
        current_body: list[str] = []
        for line in lines:
            m = re.match(r"^(##|###)\s+(.*)", line)
            if m:
                sections.append((current_title, current_body))
                current_title = m.group(2).strip()
                current_body = []
            else:
                current_body.append(line)
        sections.append((current_title, current_body))

        for i, (title, body) in enumerate(sections):
            if title.strip().lower() in _SKIP_TITLES:
                continue
            body_text = "\n".join(body).strip()
            body_text = re.sub(r"\n{3,}", "\n\n", body_text)
            if len(body_text) < _MIN_SECTION_CHARS:
                continue
            chunks.append({
                "id": f"patt:{path.relative_to(root)}:{i}",
                "source": "payloads_all_the_things",
                "title": f"{h1} — {title}" if title != h1 else h1,
                "text": f"{h1} > {title}\n\n{body_text[:2500]}",
                "tags": [topic, h1],
                "url": f"https://github.com/swisskyrepo/PayloadsAllTheThings/blob/master/{path.relative_to(root)}",
            })
    return chunks


def parse_sft_knowledge(*jsonl_paths: Path) -> list[dict]:
    """Fourth source: general-knowledge SFT rows rerouted out of training data (2026-09-10 —
    bucket1_cut + bucket2_to_rag from the empty-think bulk-pool triage, see
    DATA_IMPROVEMENT_PLAN_2026-09-09.md Workstream A). These are static Q&A (CVE summaries,
    generic security facts) with empty `<think>` blocks — useful as retrievable reference, not
    as decision-making training signal, so they move here instead of staying in the SFT corpus.
    Drops CVE-database `** RESERVED **` stubs (zero information, ~13% of the CVE-fact rows) and
    empty answers; keeps everything else as one chunk per Q&A pair."""
    import json

    chunks = []
    for path in jsonl_paths:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                msgs = {m["role"]: m["content"] for m in row["messages"]}
                question = _clean(msgs.get("user", ""))
                answer_raw = msgs.get("assistant", "")
                answer = _clean(answer_raw.split("</think>", 1)[-1]) if "</think>" in answer_raw else _clean(answer_raw)
                if not answer or "RESERVED" in answer:
                    continue
                meta = row.get("meta") or {}
                # embedding server's context window is 2048 tokens. Dense code content tokenizes
                # at ~1.7-2 chars/token (worse than prose's ~4), so cap the FINAL combined text
                # hard at 2800 chars (~1400-1650 tokens at the worst observed ratio) rather than
                # trusting a per-field char budget -- two rebuild attempts at looser caps (5500,
                # then still failing) both overflowed on real code-heavy rows.
                question = question[:800]
                answer = answer[:2000]
                text = f"Q: {question}\nA: {answer}"[:2800]
                chunks.append({
                    "id": f"sft_knowledge:{meta.get('origin', 'unknown')}:{meta.get('row_id', i)}",
                    "source": "sft_knowledge",
                    "title": question[:120],
                    "text": text,
                    "tags": [meta.get("origin", "unknown")],
                    "url": "",
                })
    return chunks


def parse_hacktricks(root: Path) -> list[dict]:
    """HackTricks wiki: comprehensive pentest methodology, privesc, web attacks, etc.
    Markdown-based, split by ## / ### headers like PayloadsAllTheThings but with
    richer topic hierarchy. Skips images, .gitbook assets, and navigation-only sections."""
    chunks = []
    skip_dirs = {".git", ".github", ".gitbook", "images", "imgs", "assets", "node_modules"}
    skip_titles_lower = _SKIP_TITLES | {"readme", "page", "untitled", "index"}

    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") or part in skip_dirs for part in path.parts):
            continue

        rel = path.relative_to(root)
        # derive topic from path hierarchy (e.g. "linux-hardening/privilege-escalation")
        topic_parts = [p for p in rel.parent.parts if p not in skip_dirs and p != "src"]
        topic = "/".join(topic_parts) if topic_parts else rel.stem

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if len(text) < 60:
            continue

        lines = text.splitlines()
        h1 = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), path.stem)

        sections: list[tuple[str, list[str]]] = []
        current_title = h1
        current_body: list[str] = []
        for line in lines:
            m = re.match(r"^(##|###)\s+(.*)", line)
            if m:
                sections.append((current_title, current_body))
                current_title = m.group(2).strip()
                current_body = []
            else:
                current_body.append(line)
        sections.append((current_title, current_body))

        for i, (title, body) in enumerate(sections):
            if title.strip().lower() in skip_titles_lower:
                continue
            body_text = "\n".join(body).strip()
            body_text = re.sub(r"\n{3,}", "\n\n", body_text)
            # skip gitbook hints/embeds that are pure navigation
            body_text = re.sub(r"\{%\s*(?:hint|embed|content-ref)[^%]*%\}[^{]*\{%\s*end\w*\s*%\}", "", body_text)
            body_text = re.sub(r"\{%\s*(?:hint|embed|content-ref)[^%]*%\}", "", body_text)
            body_text = body_text.strip()
            if len(body_text) < _MIN_SECTION_CHARS:
                continue
            chunks.append({
                "id": f"hacktricks:{rel}:{i}",
                "source": "hacktricks",
                "title": f"{h1} — {title}" if title != h1 else h1,
                "text": f"HackTricks: {topic} > {title}\n\n{body_text[:2500]}",
                "tags": [t for t in topic.split("/") if t] + [h1],
                "url": f"https://book.hacktricks.wiki/{rel}",
            })
    return chunks


def parse_exploit_code(root: Path) -> list[dict]:
    """Exploit code snippets: reverse shells, privesc patterns, web payloads.
    YAML files with structured entries, each becoming one RAG chunk."""
    chunks = []
    for path in sorted(root.glob("*.yml")):
        try:
            entries = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            title = entry.get("title", f"{path.stem}_{i}")
            code = entry.get("code", "")
            notes = entry.get("notes", "")
            tags = entry.get("tags", [])
            variants = entry.get("variants", [])
            text = f"Exploit Code: {title}\n\n{code}"
            if notes:
                text += f"\nNotes: {notes}"
            for v in (variants or []):
                text += f"\n\nVariant — {v.get('name', '')}:\n{v.get('code', '')}"
            text = text[:2800]
            chunks.append({
                "id": f"exploit_code:{path.stem}:{i}",
                "source": "exploit_code",
                "title": title,
                "text": text,
                "tags": [str(t) for t in tags],
                "url": "",
            })
    return chunks


def parse_cheatsheets(root: Path) -> list[dict]:
    """Tool syntax cheatsheets: quick-reference entries for common pentest tools.
    Each tool's entries become individual RAG chunks for precise retrieval."""
    chunks = []
    for path in sorted(root.glob("*.yml")):
        try:
            tools = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(tools, list):
            continue
        for tool_block in tools:
            tool_name = tool_block.get("tool", "unknown")
            tool_title = tool_block.get("title", tool_name)
            tags = tool_block.get("tags", [tool_name])
            for j, entry in enumerate(tool_block.get("entries", [])):
                name = entry.get("name", "")
                cmd = entry.get("cmd", "")
                note = entry.get("note", "")
                text = f"Cheatsheet: {tool_title} — {name}\nCommand:\n{cmd}"
                if note:
                    text += f"\nNote: {note}"
                text = text[:2800]
                chunks.append({
                    "id": f"cheatsheet:{tool_name}:{j}",
                    "source": "cheatsheet",
                    "title": f"{tool_name} — {name}",
                    "text": text,
                    "tags": [str(t) for t in tags] + [tool_name],
                    "url": "",
                })
    return chunks


def parse_wadcoms(root: Path) -> list[dict]:
    """WADComs: Windows/AD command reference (GTFOBins equivalent for AD).
    Structured YAML with command, description, services, and ATT&CK mapping."""
    chunks = []
    wad_dir = root / "_wadcoms"
    if not wad_dir.exists():
        return chunks
    for path in sorted(wad_dir.iterdir()):
        if not path.is_file() or path.suffix not in (".md", ".yml", ".yaml", ""):
            continue
        raw = path.read_text(encoding="utf-8", errors="ignore")
        # WADComs uses Jekyll front matter (---\n...\n---) + body
        parts = raw.split("---", 2)
        if len(parts) < 3:
            continue
        try:
            meta = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            continue
        if not meta:
            continue
        name = meta.get("name", path.stem)
        description = meta.get("description", "")
        command = meta.get("command", "")
        services = meta.get("services", [])
        attack = meta.get("attack_types", meta.get("attack", []))
        os_target = meta.get("OS", ["Windows"])
        body = parts[2].strip()
        text = f"WADComs: {name}\n{description}\n"
        if command:
            text += f"Command:\n{command}\n"
        if body:
            text += f"\n{body[:1500]}"
        text = text[:2800]
        tags = [name] + [str(s) for s in services] + [str(a) for a in attack] + [str(o) for o in os_target]
        chunks.append({
            "id": f"wadcoms:{path.stem}",
            "source": "wadcoms",
            "title": f"WADComs: {name}",
            "text": text,
            "tags": tags,
            "url": f"https://wadcoms.github.io/wadcoms/{name}/",
        })
    return chunks


def parse_hijacklibs(root: Path) -> list[dict]:
    """HijackLibs: DLL hijacking database. Each entry is a YAML file describing
    a vulnerable binary, the DLL it loads, and the hijack technique."""
    chunks = []
    yml_dir = root / "yml"
    if not yml_dir.exists():
        return chunks
    for path in sorted(yml_dir.rglob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not doc or not isinstance(doc, dict) or "Name" not in doc:
            continue
        name = doc.get("Name", path.stem)
        vendor = doc.get("Vendor", "")
        exp_locs = doc.get("ExpectedLocations", [])
        vuln_exes = doc.get("VulnerableExecutables", [])
        text = f"HijackLibs: {name}\nVendor: {vendor}\n"
        if exp_locs:
            text += f"Expected locations: {', '.join(str(e) for e in exp_locs)}\n"
        for ve in (vuln_exes or [])[:5]:
            if isinstance(ve, dict):
                text += f"\nVulnerable EXE: {ve.get('Path', '')} — {ve.get('Type', '')} hijack"
        text = text[:2800]
        tags = [name, vendor, "dll-hijack", "windows", "persistence"]
        chunks.append({
            "id": f"hijacklibs:{path.stem}",
            "source": "hijacklibs",
            "title": f"DLL Hijack: {name}",
            "text": text,
            "tags": [str(t) for t in tags],
            "url": f"https://hijacklibs.net/entries/{path.stem}.html",
        })
    return chunks


def parse_loldrivers(root: Path) -> list[dict]:
    """LOLDrivers: vulnerable/malicious driver database.
    YAML files with driver info, hashes, and known-vulnerable details."""
    chunks = []
    yml_dir = root / "yaml"
    if not yml_dir.exists():
        yml_dir = root / "loldrivers.io" / "content" / "drivers"
    if not yml_dir.exists():
        return chunks
    for path in sorted(yml_dir.rglob("*.yaml")) + sorted(yml_dir.rglob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not doc or not isinstance(doc, dict):
            continue
        name = doc.get("Name", "")
        if not name:
            tags_list = doc.get("Tags", [])
            name = tags_list[0] if tags_list else path.stem
        category = doc.get("Category", "")
        commands = doc.get("Commands", {})
        text = f"LOLDriver: {name}\nCategory: {category}\n"
        if isinstance(commands, dict):
            desc = commands.get("Description", "")
            usecase = commands.get("Usecase", "")
            privileges = commands.get("Privileges", "")
            if desc:
                text += f"Description: {desc}\n"
            if usecase:
                text += f"Use case: {usecase}\n"
            if privileges:
                text += f"Privileges: {privileges}\n"
        elif isinstance(commands, list):
            for c in commands[:3]:
                if isinstance(c, dict):
                    text += f"Description: {c.get('Description', '')}\n"
        samples = doc.get("KnownVulnerableSamples", [])
        for s in (samples or [])[:2]:
            if isinstance(s, dict):
                desc = s.get("Description", "")
                company = s.get("Company", "")
                if desc or company:
                    text += f"\nSample: {company} — {desc}"
        text = text[:2800]
        tags = [str(t) for t in doc.get("Tags", [])] + ["driver", "windows", "kernel"]
        chunks.append({
            "id": f"loldrivers:{path.stem}",
            "source": "loldrivers",
            "title": f"LOLDriver: {name}",
            "text": text,
            "tags": tags,
            "url": f"https://www.loldrivers.io/drivers/{path.stem}/",
        })
    return chunks


def parse_atomic_red_team(root: Path) -> list[dict]:
    """Atomic Red Team: real command templates mapped to MITRE ATT&CK techniques.
    Each atomics/<technique>/T*.yaml has test definitions with actual commands."""
    chunks = []
    atomics_dir = root / "atomics"
    if not atomics_dir.exists():
        return chunks
    for tech_dir in sorted(atomics_dir.iterdir()):
        if not tech_dir.is_dir() or not tech_dir.name.startswith("T"):
            continue
        yaml_file = tech_dir / f"{tech_dir.name}.yaml"
        if not yaml_file.exists():
            continue
        try:
            doc = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not doc or "atomic_tests" not in doc:
            continue
        technique_id = doc.get("attack_technique", tech_dir.name)
        technique_name = doc.get("display_name", technique_id)
        for i, test in enumerate(doc.get("atomic_tests", [])):
            test_name = test.get("name", f"test_{i}")
            description = test.get("description", "")
            platforms = test.get("supported_platforms", [])
            executor = test.get("executor", {})
            exec_cmd = executor.get("command", "")
            cleanup = executor.get("cleanup_command", "")
            exec_name = executor.get("name", "")
            inputs = test.get("input_arguments", {})
            if not exec_cmd:
                continue
            text = f"Atomic Red Team: {technique_id} — {technique_name}\nTest: {test_name}\n"
            text += f"Platform: {', '.join(str(p) for p in platforms)}\n"
            text += f"Executor: {exec_name}\n"
            text += f"Description: {description[:500]}\n"
            text += f"Command:\n{exec_cmd[:1200]}\n"
            if cleanup:
                text += f"\nCleanup:\n{cleanup[:500]}"
            if inputs:
                text += "\nInputs: " + ", ".join(f"{k}={v.get('default','')}" for k, v in inputs.items() if isinstance(v, dict))
            text = text[:2800]
            tags = [technique_id, technique_name] + [str(p) for p in platforms]
            chunks.append({
                "id": f"atomic:{technique_id}:{i}",
                "source": "atomic_red_team",
                "title": f"{technique_id}: {test_name}",
                "text": text,
                "tags": tags,
                "url": f"https://github.com/redcanaryco/atomic-red-team/blob/master/atomics/{technique_id}/{technique_id}.yaml",
            })
    return chunks


def parse_seclists(root: Path) -> list[dict]:
    """SecLists: curated security payload/wordlist collections.
    We only parse the payload and discovery sections (not raw wordlists) for
    interesting technique descriptions and fuzzing payloads."""
    chunks = []
    interesting_dirs = [
        "Fuzzing", "Pattern-Matching", "Payloads",
    ]
    for dir_name in interesting_dirs:
        target = root / dir_name
        if not target.exists():
            continue
        for path in sorted(target.rglob("*.txt")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
            if len(lines) < 3 or len(lines) > 500:
                continue
            rel = path.relative_to(root)
            title = f"SecLists: {rel}"
            content = f"SecLists: {rel}\nPayloads ({len(lines)} entries):\n"
            content += "\n".join(lines[:80])
            content = content[:2800]
            tags = [dir_name, path.stem, "payloads", "fuzzing"]
            chunks.append({
                "id": f"seclists:{rel}",
                "source": "seclists",
                "title": title,
                "text": content,
                "tags": [str(t) for t in tags],
                "url": f"https://github.com/danielmiessler/SecLists/blob/master/{rel}",
            })
    return chunks


def parse_external_datasets(ext_dir: Path) -> list[dict]:
    """Parse the 4 downloaded external cybersecurity datasets into RAG chunks.
    - CyberStrike (121K): offensive/red-team QA pairs
    - Fenrir (100K): OWASP/crypto/defensive QA
    - ExploitDB (70K): real CVE exploit descriptions
    - NIST (425K): cybersecurity standards text chunks
    """
    import json
    import hashlib

    chunks = []
    seen = set()

    def _dedup_add(chunk):
        h = hashlib.md5(chunk["text"][:500].encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            chunks.append(chunk)

    # CyberStrike — messages format [{role, content}, ...]
    cs_path = ext_dir / "cyberstrike_raw.jsonl"
    if cs_path.exists():
        with open(cs_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                row = json.loads(line)
                msgs = {m["role"]: m["content"] for m in row.get("messages", [])}
                q = _clean(msgs.get("user", ""))
                a = _clean(msgs.get("assistant", ""))
                if not a or len(a) < 50 or "I cannot" in a[:30]:
                    continue
                text = f"Q: {q[:800]}\nA: {a[:2000]}"[:2800]
                _dedup_add({
                    "id": f"ext_cyberstrike:{i}",
                    "source": "ext_cyberstrike",
                    "title": q[:120],
                    "text": text,
                    "tags": ["cyberstrike", "red-team", "offensive"],
                    "url": "",
                })

    # Fenrir — system/user/assistant keys
    fn_path = ext_dir / "fenrir_raw.jsonl"
    if fn_path.exists():
        with open(fn_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                row = json.loads(line)
                q = _clean(row.get("user", ""))
                a = _clean(row.get("assistant", ""))
                if not a or len(a) < 50:
                    continue
                text = f"Q: {q[:800]}\nA: {a[:2000]}"[:2800]
                _dedup_add({
                    "id": f"ext_fenrir:{i}",
                    "source": "ext_fenrir",
                    "title": q[:120],
                    "text": text,
                    "tags": ["fenrir", "defensive", "owasp"],
                    "url": "",
                })

    # ExploitDB — input/output keys
    edb_path = ext_dir / "exploitdb_raw.jsonl"
    if edb_path.exists():
        with open(edb_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                row = json.loads(line)
                q = _clean(row.get("input", ""))
                a = _clean(row.get("output", ""))
                if not a or len(a) < 30:
                    continue
                text = f"Q: {q[:800]}\nA: {a[:2000]}"[:2800]
                _dedup_add({
                    "id": f"ext_exploitdb:{i}",
                    "source": "ext_exploitdb",
                    "title": q[:120],
                    "text": text,
                    "tags": ["exploitdb", "cve", "exploit"],
                    "url": "",
                })

    # NIST — text chunks (already chunked, use directly)
    nist_path = ext_dir / "nist_raw.jsonl"
    if nist_path.exists():
        with open(nist_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                row = json.loads(line)
                text_raw = row.get("text", "")
                if not text_raw or len(text_raw) < 60:
                    continue
                # NIST rows start with a 280-char system prompt (no newlines) — strip it
                if text_raw.startswith("You are a cybersecurity expert"):
                    marker = "related documents."
                    idx = text_raw.find(marker)
                    if idx > 0:
                        text_raw = text_raw[idx + len(marker):].strip()
                    else:
                        text_raw = text_raw[280:].strip()
                text = _clean(text_raw)[:2800]
                if len(text) < 60:
                    continue
                meta_raw = row.get("metadata", {})
                if isinstance(meta_raw, str):
                    try:
                        meta_raw = json.loads(meta_raw)
                    except json.JSONDecodeError:
                        meta_raw = {}
                source_doc = meta_raw.get("source", "nist") if isinstance(meta_raw, dict) else "nist"
                _dedup_add({
                    "id": f"ext_nist:{i}",
                    "source": "ext_nist",
                    "title": text[:120],
                    "text": text,
                    "tags": ["nist", "standards", "compliance", str(source_doc)[:50]],
                    "url": "",
                })

    return chunks


def parse_all(corpus_src: Path) -> list[dict]:
    chunks = []
    chunks += parse_gtfobins(corpus_src / "gtfobins")
    chunks += parse_lolbas(corpus_src / "lolbas")
    chunks += parse_payloads_all_the_things(corpus_src / "patt")
    ht_path = corpus_src / "hacktricks"
    if ht_path.exists():
        chunks += parse_hacktricks(ht_path)
    ec_path = corpus_src / "exploit_code"
    if ec_path.exists():
        chunks += parse_exploit_code(ec_path)
    cs_path = corpus_src / "cheatsheets"
    if cs_path.exists():
        chunks += parse_cheatsheets(cs_path)
    wad_path = corpus_src / "wadcoms"
    if wad_path.exists():
        chunks += parse_wadcoms(wad_path)
    hl_path = corpus_src / "hijacklibs"
    if hl_path.exists():
        chunks += parse_hijacklibs(hl_path)
    ld_path = corpus_src / "loldrivers"
    if ld_path.exists():
        chunks += parse_loldrivers(ld_path)
    art_path = corpus_src / "atomic_red_team"
    if art_path.exists():
        chunks += parse_atomic_red_team(art_path)
    sl_path = corpus_src / "seclists"
    if sl_path.exists():
        chunks += parse_seclists(sl_path)
    return chunks
