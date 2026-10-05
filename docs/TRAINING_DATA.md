# Training Data (v3.2)

> **Scope of this document.** It describes the composition of the training corpus. The corpus
> itself is **not in this repository** — only a 1,000-row train sample and a 100-row eval sample
> are bundled (see [`datasets/README.md`](../datasets/README.md)); the full set is gated alongside
> the model weights. Nothing here can be verified against the repository, so treat the breakdown
> as the author's description of a private artifact rather than an independently checkable claim.
>
> **Version.** This document covers the **v3.2** corpus. The checkpoint behind the committed
> AutoPenBench run was trained on the older **v2** data, and current training uses **v4**. The
> per-component breakdown below has not been updated for v4.

## Dataset Overview

| Component | Train | Eval | Total |
|-----------|-------|------|-------|
| Pentest QA (custom) | 27,046 | 2,097 | 29,143 |
| External Security QA | 52,700 | 2,100 | 54,800 |
| Code SFT | 20,890 | 1,100 | 21,990 |
| Agentic / Multi-turn | 22,811 | 1,201 | 24,012 |
| **Total** | **123,447** | **6,498** | **129,945** |

> **Known discrepancy, unreconciled.** These component figures sum to 123,447 train / 6,498 eval,
> but [`datasets/README.md`](../datasets/README.md) states 123,416 train / 6,447 eval for the
> shipped artifact — a difference of 31 train and 51 eval rows. The cause has not been traced
> (plausibly a filtering pass applied after this table was written, but that is a guess, not a
> finding). Both figures are reproduced as-is rather than silently harmonised; the released
> dataset card will be the authoritative count.

The corpus is pre-tokenized in Arrow format for training. That artifact is likewise not in this
repository.

## Data Sources

### Custom Pentest QA

Generated from writeups of HackTheBox, TryHackMe, and VulnHub, converted into instruction-response pairs. Each pair covers a specific vulnerability with the complete exploit procedure. All responses include `<think>` tags for chain-of-thought reasoning and tool calls in `<tool_call>` format matching the inference-time parser.

### External Security Knowledge

| Dataset | Rows | Content |
|---------|------|---------|
| CyberStrike | ~30K | Cybersecurity instruction-response |
| Fenrir | ~25K | Pentesting knowledge |
| ExploitDB | ~15K | Exploit descriptions |
| NIST NVD | ~10K | Vulnerability intelligence |

Quality filters applied: minimum response length, deduplication by MD5 hash of the first 500 characters, filtering of generic refusal responses, and context-length capping.

### Code SFT

Code instruction-response pairs from Magicoder (OSS-Instruct and Evol-Instruct), CodeFeedback (multi-turn debugging), Glaive Code (diverse instruction-response pairs), and Self OSS Instruct (self-generated programming exercises).

### Multi-Turn Conversations

8,442 unique conversations (16,085 multi-turn rows) simulating real penetration testing scenarios:

| Type | Model | Count | Description |
|------|-------|-------|-------------|
| Full Pentest | Qwen3-32B | 1,205 | recon → vuln scan → exploit → post-exploit |
| Tool Calling | Qwen3-32B | 1,318 | nmap, gobuster, sqlmap, metasploit |
| Error Recovery | Qwen3-32B | 1,102 | handling exploit failures and credential errors |
| RAG-Assisted | DeepSeek V3.2 | 1,240 | using knowledge base during pentest |
| Lateral Movement | DeepSeek V3.2 | 1,156 | pivoting, port forwarding, credential reuse |
| Web App Exploit | DeepSeek V3.2 | 1,221 | SQLi, XSS, file upload, SSRF, IDOR |
| CTF Challenge | DeepSeek V3.2 | 1,200 | binary exploitation, crypto, forensics |

## Curriculum Learning

Training data is ordered by progressive difficulty across four phases:

| Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|---------|---------|---------|---------|
| External Security QA | Pentest QA | Code SFT | Agentic / Multi-turn |
| 52,700 | 27,046 | 20,890 | 22,811 |

**Epoch 1** processes all rows in curriculum order for difficulty progression. **Epoch 2** processes the same rows in fully shuffled order for generalization. Total: 246,894 training rows.

## Decontamination Pipeline

> ### ⚠ Specified, not implemented
>
> **There is no decontamination code in this repository.** The five stages below are a
> specification for work that has not been written, and no overlap statistics have been produced.
>
> This matters more than most of the gaps in this project, and it should be read as a caveat on
> the benchmark number rather than as a detail. The training corpus is built partly from
> HackTheBox, TryHackMe and VulnHub writeups; AutoPenBench is built from comparable vulnerable
> machines. Without a decontamination pass actually run and reported, **there is no evidence that
> the 8/33 AutoPenBench result is free of train/test overlap** — the model may have seen a
> writeup of a benchmark scenario during SFT. The result should be treated as uncontrolled for
> contamination until stage 5 below produces real numbers.
>
> Implementing this is the prerequisite for the benchmark figure meaning anything, and it comes
> before any further capability work.

The intended pipeline, because training data originates from sources that overlap with benchmark
evaluation targets:

1. **CVE-level deduplication** — All CVE IDs in training data are compared against CVEs in AutoPenBench and AI-Pentest-Benchmark; overlapping rows are removed.
2. **Machine-name matching** — Machine names and lab identifiers from writeups are matched against benchmark targets.
3. **N-gram overlap** — Textual overlap between training data and benchmark descriptions is measured and flagged.
4. **Hold-out protocol** — Training examples that are "near" a benchmark scenario (same service + same vulnerability class) are held out to the test set.
5. **Overlap statistics** — Before/after decontamination overlap percentages to be measured and
   published alongside any benchmark result that depends on them. **No such statistics exist yet.**

```
Raw Sources → Quality Filters → Format Conversion → Decontamination → Curriculum Assembly
```
