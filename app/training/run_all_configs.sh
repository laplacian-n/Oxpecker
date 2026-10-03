#!/bin/bash
# Run all 3 LoRA configs sequentially on H200
# Total: ~6 hours, ~$28

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "PentestAI — LoRA Training Suite (3 configs)"
echo "GPU: H200 141GB | BF16 | Qwen3-32B"
echo "=========================================="
echo ""

# Config A: Standard LoRA (r=32, q/v only)
# ~1.5 hr
echo "[1/3] Config A: Standard (r=32, q_proj + v_proj)"
echo "---"
bash "$DIR/run_lora_h200.sh" standard
echo ""

# Config B: Aggressive LoRA (r=128, all layers, DoRA, rsLoRA)
# ~2 hr
echo "[2/3] Config B: Aggressive (r=128, all layers, DoRA + rsLoRA)"
echo "---"
bash "$DIR/run_lora_h200.sh" aggressive
echo ""

# Config C: Max LoRA (r=256, all layers, DoRA, rsLoRA)
# ~2.5 hr
echo "[3/3] Config C: Max (r=256, all layers, DoRA + rsLoRA)"
echo "---"
bash "$DIR/run_lora_h200.sh" max
echo ""

echo "=========================================="
echo "All 3 configs complete!"
echo "Models saved in:"
echo "  outputs/lora-standard-r32/final/"
echo "  outputs/lora-aggressive-r128/final/"
echo "  outputs/lora-max-r256/final/"
echo "=========================================="
