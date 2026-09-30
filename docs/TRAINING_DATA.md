# Training Data (v3.2)

## Dataset Overview

| Component | Train | Eval | Total |
|-----------|-------|------|-------|
| Pentest QA (custom) | 27,046 | 2,097 | 29,143 |
| External Security QA | 52,700 | 2,100 | 54,800 |
| Code SFT | 20,890 | 1,100 | 21,990 |
| Agentic / Multi-turn | 22,811 | 1,201 | 24,012 |
| **Total** | **123,447** | **6,498** | **129,945** |

The dataset is finalized and pre-tokenized in Arrow format, ready for immediate training.

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

Because training data originates from sources that overlap with benchmark evaluation targets, we implement a multi-stage decontamination pipeline:

1. **CVE-level deduplication** — All CVE IDs in training data are compared against CVEs in AutoPenBench and AI-Pentest-Benchmark; overlapping rows are removed.
2. **Machine-name matching** — Machine names and lab identifiers from writeups are matched against benchmark targets.
3. **N-gram overlap** — Textual overlap between training data and benchmark descriptions is measured and flagged.
4. **Hold-out protocol** — Training examples that are "near" a benchmark scenario (same service + same vulnerability class) are held out to the test set.
5. **Overlap statistics** — Before and after decontamination overlap percentages are reported.

```
Raw Sources → Quality Filters → Format Conversion → Decontamination → Curriculum Assembly
```
