# Oxpecker Dataset

## Overview

Curated cybersecurity dataset for fine-tuning LLMs on penetration testing tasks.

| Split | Rows | Size | Description |
|-------|------|------|-------------|
| Train (full) | 123,416 | ~612 MB | Full training set (not included — see below) |
| Train (sample) | 1,000 | ~4.7 MB | Representative sample included in this repo |
| Eval (full) | 6,447 | ~33 MB | Full evaluation set (not included) |
| Eval (sample) | 100 | ~566 KB | Representative sample included in this repo |

Only the two sample files are verifiable from this repository; the full-split row counts describe
a private artifact. Note also that [`docs/TRAINING_DATA.md`](../docs/TRAINING_DATA.md) gives
123,447 train / 6,498 eval for its per-component breakdown — 31 and 51 rows above the figures
here. That discrepancy has not been traced, and both are reproduced rather than harmonised.

**No decontamination against evaluation benchmarks has been applied to this data yet.** The
intended pipeline is specified in `docs/TRAINING_DATA.md`, but it is not implemented, so any
benchmark result obtained with this corpus is uncontrolled for train/test overlap.

## Format

Each line is a JSON object in chat-completion format:

```json
{
  "messages": [
    {"role": "system", "content": "You are a penetration testing assistant..."},
    {"role": "user", "content": "<task description>"},
    {"role": "assistant", "content": "<detailed response with commands and analysis>"}
  ]
}
```

## Data Sources

The training data is curated from multiple cybersecurity sources including:

- CTF writeups and walkthroughs
- Penetration testing methodology guides
- Tool documentation and usage examples
- Vulnerability analysis and exploitation techniques
- Post-exploitation and privilege escalation scenarios

All data is sourced from publicly available educational and training materials.

## Samples

The `sample_train.jsonl` and `sample_eval.jsonl` files contain representative subsets for inspection and testing. These samples include simulated credentials, SSH keys, and passwords as part of penetration testing scenarios — these are synthetic training examples, not real secrets.

## Full Dataset

The full dataset will be released on HuggingFace upon publication. Contact the author for early access.

## License

Apache 2.0 — see [LICENSE](../LICENSE).
