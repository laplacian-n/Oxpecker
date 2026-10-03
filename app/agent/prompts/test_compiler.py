"""Prompt registry golden tests — M4.7. Digest stability (same input → same digest, always),
budget fit against the live model's actual tokenizer/context window (reusing the same
/apply-template → /tokenize mechanism budget.py already relies on, not a separate estimate),
and a schema-validation check (an unset template variable fails loudly, not silently).
Run directly: `python3 -m agent.prompts.test_compiler`.
"""
from __future__ import annotations

import jinja2

from .. import config
from ..engagement.store import PHASES
from ..llama_client import LlamaClient
from .compiler import MAX_SYSTEM_PROMPT_CHARS, UnknownPhaseError, compile_prompt

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    context = {
        "workspace_root": "/tmp/golden-test-workspace", "security_tools_enabled": True,
        "in_scope_targets": "demo.example.test, 10.0.0.0/24",
    }

    print("== Digest stability (golden test) ==")
    r1 = compile_prompt(context)
    r2 = compile_prompt(context)
    check("compiling the same context twice yields the same digest", r1.digest == r2.digest)
    check("layer digests are also stable across compiles", r1.layer_digests == r2.layer_digests)

    print("\n== Digest sensitivity (changes are detected) ==")
    r3 = compile_prompt({**context, "security_tools_enabled": False})
    check(
        "changing a template variable changes the digest",
        r3.digest != r1.digest,
    )
    check(
        "only the engagement layer's digest changes, not the static layers",
        r3.layer_digests["engagement.md.j2"] != r1.layer_digests["engagement.md.j2"]
        and r3.layer_digests["core_identity.md"] == r1.layer_digests["core_identity.md"],
    )

    print("\n== Strict undefined variables fail loudly ==")
    try:
        compile_prompt({"workspace_root": "/tmp/x"})  # missing security_tools_enabled
        check("missing template variable raises, not silently renders blank", False)
    except jinja2.UndefinedError:
        check("missing template variable raises, not silently renders blank", True)

    print("\n== Phase/tool layers are backward-compatible additions ==")
    r_explicit_none = compile_prompt(context, phase=None, active_tools=None)
    check(
        "explicit phase=None/active_tools=None matches the no-argument call exactly",
        r_explicit_none.digest == r1.digest,
    )

    print("\n== Every pipeline phase has a real prompt file and compiles ==")
    for phase in PHASES:
        r_phase = compile_prompt(context, phase=phase)
        layer_key = f"phases/{phase.lower()}.md"
        check(
            f"phase {phase!r} compiles and adds a non-empty phases/ layer",
            layer_key in r_phase.layer_digests and len(r_phase.text) > len(r1.text),
        )

    print("\n== False-positive discipline is in the ANALYSIS/VALIDATION prompts ==")
    # Added 2026-09-06 after a real autonomous run against demo.owasp-juice.shop 'confirmed' a
    # Heroku edge-routing artifact (port 8080 == the same app) as an "alternative service"
    # finding. The 5-model comparison showed every quant/abliteration variant gets this right
    # when the comparison is framed — so the fix is the prompt, not the model.
    analysis_text = compile_prompt(context, phase="ANALYSIS").text.lower()
    validation_text = compile_prompt(context, phase="VALIDATION").text.lower()
    check(
        "ANALYSIS prompt distinguishes 'consistent with' from 'evidence for' a specific claim",
        "consistent with it" in analysis_text and "8080" in analysis_text,
    )
    check(
        "VALIDATION prompt requires ruling out the mundane explanation before 'confirmed'",
        "mundane explanation" in validation_text
        and ("content-length" in validation_text or "etag" in validation_text),
    )

    print("\n== In-scope targets are stated in the system prompt (security sessions) ==")
    check(
        "compiled prompt names the concrete in-scope targets when given",
        "demo.example.test" in r1.text and "10.0.0.0/24" in r1.text,
    )
    check(
        "no in-scope-targets line when security tools are off",
        "demo.example.test"
        not in compile_prompt({**context, "security_tools_enabled": False}).text,
    )
    check(
        "empty in_scope_targets is harmless (no dangling 'In scope for this engagement:' line)",
        "In scope for this engagement"
        not in compile_prompt({**context, "in_scope_targets": ""}).text,
    )

    print("\n== Unknown phase name fails loudly ==")
    try:
        compile_prompt(context, phase="EXPLOIT_EVERYTHING")
        check("unknown phase raises UnknownPhaseError", False)
    except UnknownPhaseError:
        check("unknown phase raises UnknownPhaseError", True)

    print("\n== Tool cards: known tool included, unknown tool silently skipped ==")
    r_tools = compile_prompt(context, active_tools=["http_recon", "not_a_real_tool"])
    check(
        "a tool with a card contributes a layer",
        "tools/http_recon.md" in r_tools.layer_digests,
    )
    check(
        "a tool name with no card file is skipped, not an error",
        "tools/not_a_real_tool.md" not in r_tools.layer_digests,
    )

    print("\n== Phase and tool layers compose together ==")
    r_combined = compile_prompt(context, phase="RECON", active_tools=["http_recon", "port_discovery"])
    check(
        "combined compile includes both the phase layer and both tool cards",
        "phases/recon.md" in r_combined.layer_digests
        and "tools/http_recon.md" in r_combined.layer_digests
        and "tools/port_discovery.md" in r_combined.layer_digests,
    )

    print("\n== Compile-time char-budget guard (no live model needed) ==")
    check(
        f"current compiled base prompt ({len(r1.text)} chars) is under the "
        f"{MAX_SYSTEM_PROMPT_CHARS}-char early-warning threshold",
        len(r1.text) < MAX_SYSTEM_PROMPT_CHARS,
        f"len={len(r1.text)}",
    )
    import logging

    class _CaptureHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    handler = _CaptureHandler()
    compiler_log = logging.getLogger("agent.prompts.compiler")
    compiler_log.addHandler(handler)
    compiler_log.setLevel(logging.WARNING)
    import agent.prompts.compiler as compiler_mod

    original_budget = compiler_mod.MAX_SYSTEM_PROMPT_CHARS
    try:
        compiler_mod.MAX_SYSTEM_PROMPT_CHARS = 10  # force the guard to trip
        compile_prompt(context)
    finally:
        compiler_mod.MAX_SYSTEM_PROMPT_CHARS = original_budget
        compiler_log.removeHandler(handler)
    check(
        "guard logs a warning when the compiled prompt exceeds the configured budget",
        any("early-warning budget" in r.getMessage() for r in handler.records),
    )

    print("\n== Compiled prompt fits the model's actual context budget ==")
    try:
        client = LlamaClient()
        messages = [{"role": "system", "content": r1.text}, {"role": "user", "content": "hello"}]
        n_ctx = client.n_ctx()
        rendered_tokens = client.rendered_token_count(messages)
        check(
            f"compiled system prompt ({rendered_tokens} tokens) leaves headroom in a "
            f"{n_ctx}-token context window",
            rendered_tokens < n_ctx * 0.5,  # a system prompt this small should be a small slice
            f"rendered_tokens={rendered_tokens} n_ctx={n_ctx}",
        )
    except Exception as e:
        print(f"  SKIPPED  budget check (llama-server unavailable): {e}")

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
