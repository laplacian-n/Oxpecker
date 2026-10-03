#!/bin/bash
# LoRA training on RunPod H200 (141GB VRAM, single GPU)
# Usage: bash run_lora_h200.sh [standard|aggressive|max]

set -euo pipefail

PRESET="${1:-aggressive}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/../datasets/stage3_v4"
TRAIN_DATA="$DATA_DIR/train_v32_merged_train.jsonl"
EVAL_DATA="$DATA_DIR/train_v32_merged_eval.jsonl"
REPLAY_DATA="$DATA_DIR/general_replay.jsonl"
OUTPUT_DIR="$SCRIPT_DIR/../outputs"

echo "============================================"
echo "PentestAI LoRA Training — H200"
echo "Preset: $PRESET"
echo "============================================"

# ── Install dependencies (first run only) ──
pip install -q torch transformers trl peft datasets accelerate wandb \
    flash-attn --no-build-isolation 2>/dev/null

# ── Check GPU ──
python3 -c "
import torch
print(f'GPU: {torch.cuda.get_device_name(0)}')
print(f'VRAM: {torch.cuda.get_device_properties(0).total_mem / 1e9:.0f} GB')
print(f'BF16: {torch.cuda.is_bf16_supported()}')
"

# ── Optional: Login to wandb ──
# wandb login YOUR_KEY

# ── Run training ──
python3 "$SCRIPT_DIR/train_sft.py" \
    --preset "$PRESET" \
    --curriculum \
    --max_seq_len 4096 \
    --save_steps 200 \
    --eval_steps 200 \
    --train_data "$TRAIN_DATA" \
    --eval_data "$EVAL_DATA" \
    --replay_data "$REPLAY_DATA" \
    --output_dir "$OUTPUT_DIR" \
    --wandb_project "pentestai-sft"

echo ""
echo "============================================"
echo "Training complete! Model saved to:"
echo "  $OUTPUT_DIR/lora-${PRESET}-*/final/"
echo "============================================"
