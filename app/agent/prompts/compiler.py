"""Prompt registry compiler — M4.7 skeleton, extended this pass with the phase prompts and tool
cards M4.7 deliberately left as empty seam directories (see manifest.yaml's original comment).
Reads manifest.yaml, compiles the listed layers in fixed order (jinja2 renders `.j2` files,
plain files are read as-is), optionally appends a phase-specific layer and per-tool cards, then
concatenates everything and returns a digest of the exact compiled text — recorded in the audit
log and eval metadata so a run's *actual* system prompt is reconstructable and diffable across
changes, not just implied by "whatever loop.py's code currently does."

`phase`/`active_tools` are both optional and additive: a caller that omits them (every caller
before this pass) gets byte-identical output to before, so no existing recorded `prompt_version`
digest is invalidated by this change.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import jinja2
import yaml

from ..engagement.store import PHASES

log = logging.getLogger("agent.prompts.compiler")

PROMPTS_DIR = Path(__file__).resolve().parent

# A compile-time early-warning budget, not a hard limit enforced against the live model's real
# tokenizer (test_compiler.py's own budget check already does that, against the actual n_ctx, but
# needs a reachable llama-server — this doesn't). ~4 chars/token is a standard, conservative
# estimate for English text; Thai and other non-Latin scripts tokenize denser than that, so this
# undercounts admissible headroom for non-English content rather than overcounts it — a warning
# fires earlier than truly necessary in that case, not later. Purely a signal (logged, not
# raised): as more tool cards/phase files/layers accumulate over time, this is what catches "the
# compiled prompt quietly grew past a sane size" before someone has to notice a live budget
# failure to find out.
# 17_000 ≈ 4250 tokens (conservative 4 chars/token) — still ~half of what the real
# rendered-token budget check allows (n_ctx * 0.5 = 8192 for the 16384 ctx). Bumped from 14_000
# on 2026-09-07: the ANALYSIS and VALIDATION phase prompts deliberately grew (false-positive /
# rule-out-the-mundane-explanation discipline, added after a real autonomous run confirmed a
# Heroku edge-routing artifact as a finding). The growth was reviewed and intended — this guard
# catches *unintended* growth.
MAX_SYSTEM_PROMPT_CHARS = 17_000


@dataclass
class CompiledPrompt:
    text: str
    digest: str  # sha256 of `text` — the prompt_version recorded in audit/eval metadata
    layer_digests: dict[str, str]  # per-file digest, useful for diagnosing *which* layer changed
    manifest_version: int


class UnknownPhaseError(RuntimeError):
    pass


def compile_prompt(
    context: dict,
    prompts_dir: Path = PROMPTS_DIR,
    phase: str | None = None,
    active_tools: list[str] | None = None,
) -> CompiledPrompt:
    manifest = yaml.safe_load((prompts_dir / "manifest.yaml").read_text())
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(prompts_dir)),
        autoescape=False,  # plain text prompts, not HTML — escaping would corrupt them
        undefined=jinja2.StrictUndefined,  # an unset template variable fails loudly, not silently
    )

    sections = []
    layer_digests = {}
    for layer_name in manifest["layers"]:
        path = prompts_dir / layer_name
        if layer_name.endswith(".j2"):
            rendered = env.get_template(layer_name).render(**context)
        else:
            rendered = path.read_text()
        rendered = rendered.strip()
        sections.append(rendered)
        layer_digests[layer_name] = hashlib.sha256(rendered.encode()).hexdigest()

    # Phase-specific guidance (agent/prompts/phases/) — optional, additive. Every valid pipeline
    # phase (agent.engagement.store.PHASES) is expected to have a file once this seam is
    # populated, so an unknown phase name fails loudly rather than silently omitting guidance;
    # this is different from tool cards below, where not every tool having a card is normal.
    if phase is not None:
        if phase not in PHASES:
            raise UnknownPhaseError(f"phase must be one of {PHASES}, got {phase!r}")
        phase_layer = f"phases/{phase.lower()}.md"
        phase_path = prompts_dir / phase_layer
        if not phase_path.exists():
            raise UnknownPhaseError(f"no prompt file for phase {phase!r} at {phase_path}")
        rendered = phase_path.read_text().strip()
        sections.append(rendered)
        layer_digests[phase_layer] = hashlib.sha256(rendered.encode()).hexdigest()

    # Tool cards (agent/prompts/tools/) — optional, additive, and per-tool coverage is allowed
    # to be partial: a tool without a card yet just contributes nothing here, its JSON-schema
    # `description` is still what the model sees either way.
    if active_tools:
        for tool_name in sorted(active_tools):
            tool_layer = f"tools/{tool_name}.md"
            tool_path = prompts_dir / tool_layer
            if not tool_path.exists():
                continue
            rendered = tool_path.read_text().strip()
            sections.append(rendered)
            layer_digests[tool_layer] = hashlib.sha256(rendered.encode()).hexdigest()

    text = "\n\n".join(sections)
    if len(text) > MAX_SYSTEM_PROMPT_CHARS:
        log.warning(
            "compiled system prompt is %d chars, over the %d-char early-warning budget "
            "(phase=%r, %d active tool(s)) — check whether a layer grew unintentionally",
            len(text), MAX_SYSTEM_PROMPT_CHARS, phase, len(active_tools or []),
        )
    digest = hashlib.sha256(text.encode()).hexdigest()
    return CompiledPrompt(
        text=text, digest=digest, layer_digests=layer_digests, manifest_version=manifest["version"]
    )
