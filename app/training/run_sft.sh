#!/bin/bash
#SBATCH --job-name=pentestai-sft
#SBATCH --partition=defq
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:8
#SBATCH --cpus-per-task=56
#SBATCH --mem=180G
#SBATCH --time=24:00:00
#SBATCH --output=sft_%j.out
#SBATCH --error=sft_%j.err

# ── Environment ──
source ~/.bashrc
module load conda/miniforge3
conda activate sft

export WANDB_PROJECT="pentestai-sft"
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=7

cd ~/sft_data

# ── Option A: Curriculum training (recommended first run) ──
accelerate launch \
    --num_processes 8 \
    --num_machines 1 \
    --mixed_precision bf16 \
    --use_deepspeed \
    --deepspeed_config_file ~/training/ds_config.json \
    ~/training/train_sft.py \
    --curriculum \
    --lr 2e-5 \
    --batch_size 1 \
    --grad_accum 2 \
    --max_seq_len 2048 \
    --save_steps 500 \
    --eval_steps 500

# ── Option B: LoRA (uncomment if full SFT OOMs or forgetting is bad) ──
# accelerate launch \
#     --num_processes 8 \
#     --num_machines 1 \
#     --mixed_precision bf16 \
#     --use_deepspeed \
#     --deepspeed_config_file ~/training/ds_config.json \
#     ~/training/train_sft.py \
#     --curriculum \
#     --lora \
#     --lora_rank 64 \
#     --lora_alpha 128 \
#     --lr 1e-4 \
#     --batch_size 2 \
#     --grad_accum 2 \
#     --max_seq_len 2048 \
#     --save_steps 500 \
#     --eval_steps 500
