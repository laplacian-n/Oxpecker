#!/usr/bin/env python3
"""SFT training script for Qwen3-32B.

Supports:
  - Full SFT (DeepSpeed ZeRO-3, multi-GPU)
  - LoRA / DoRA (single GPU, BF16)
  - 3 LoRA presets: standard, aggressive, max

Usage (LoRA on H200):
  python train_sft.py --preset aggressive --max_seq_len 4096

Usage (Full SFT on DGX):
  accelerate launch --config_file ds_config.yaml train_sft.py --full_sft
"""
import argparse
import json
import torch
from pathlib import Path
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTConfig, SFTTrainer

MODEL_ID = "Qwen/Qwen3-32B"

LORA_PRESETS = {
    "standard": {
        "r": 32,
        "alpha": 64,
        "target_modules": ["q_proj", "v_proj"],
        "use_dora": False,
        "use_rslora": False,
        "init_weights": "pissa_niter_4",
        "lr": 2e-4,
        "batch_size": 4,
        "grad_accum": 8,
    },
    "aggressive": {
        "r": 128,
        "alpha": 256,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj",
                           "gate_proj", "up_proj", "down_proj"],
        "use_dora": True,
        "use_rslora": True,
        "init_weights": "pissa_niter_4",
        "lr": 1e-4,
        "batch_size": 4,
        "grad_accum": 8,
    },
    "max": {
        "r": 256,
        "alpha": 512,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj",
                           "gate_proj", "up_proj", "down_proj"],
        "use_dora": True,
        "use_rslora": True,
        "init_weights": "pissa_niter_4",
        "lr": 5e-5,
        "batch_size": 1,
        "grad_accum": 32,
    },
    "stage2": {
        "r": 64,
        "alpha": 128,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        "use_dora": True,
        "use_rslora": True,
        "init_weights": "pissa_niter_4",
        "lr": 1e-4,
        "batch_size": 4,
        "grad_accum": 8,
    },
}


def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                if "messages" in row:
                    row["messages"] = [
                        {"role": m["role"], "content": m.get("content") or ""}
                        for m in row["messages"]
                    ]
                rows.append({"messages": row["messages"]})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", choices=["standard", "aggressive", "max", "stage2"],
                        default="aggressive", help="LoRA preset config")
    parser.add_argument("--full_sft", action="store_true",
                        help="Full SFT instead of LoRA (needs multi-GPU)")
    parser.add_argument("--curriculum", action="store_true",
                        help="Use curriculum-ordered data (don't shuffle)")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--lr", type=float, default=None,
                        help="Override preset learning rate")
    parser.add_argument("--batch_size", type=int, default=None)
    parser.add_argument("--grad_accum", type=int, default=None)
    parser.add_argument("--max_seq_len", type=int, default=4096)
    parser.add_argument("--save_steps", type=int, default=200)
    parser.add_argument("--eval_steps", type=int, default=200)
    parser.add_argument("--model_path", type=str, default=None,
                        help="Local model path (default: download from HF)")
    parser.add_argument("--train_data", type=str, required=True)
    parser.add_argument("--eval_data", type=str, required=True)
    parser.add_argument("--replay_data", type=str, default=None,
                        help="General instruction data to mix in (~10%% for forgetting prevention)")
    parser.add_argument("--output_dir", type=str, default="./output")
    parser.add_argument("--wandb_project", type=str, default="pentestai-sft")
    parser.add_argument("--tokenized_dir", type=str, default=None,
                        help="Pre-tokenized Arrow dataset dir (skip tokenization)")
    args = parser.parse_args()

    model_path = args.model_path or MODEL_ID
    use_pretokenized = args.tokenized_dir is not None

    # ── Load data ──
    if use_pretokenized:
        from datasets import load_from_disk
        train_dataset = load_from_disk(f"{args.tokenized_dir}/train")
        eval_dataset = load_from_disk(f"{args.tokenized_dir}/eval")
        print(f"Loaded pre-tokenized data from {args.tokenized_dir}")
        print(f"Train: {len(train_dataset):,} rows")
        print(f"Eval:  {len(eval_dataset):,} rows")
    else:
        train_data = load_jsonl(args.train_data)
        eval_data = load_jsonl(args.eval_data)

        if args.replay_data:
            import random
            replay_data = load_jsonl(args.replay_data)
            train_data.extend(replay_data)
            if not args.curriculum:
                random.shuffle(train_data)
            print(f"Replay: {len(replay_data):,} rows mixed in ({len(replay_data)/len(train_data)*100:.1f}%)")

        print(f"Train: {len(train_data):,} rows")
        print(f"Eval:  {len(eval_data):,} rows")

        train_dataset = Dataset.from_list(train_data)
        eval_dataset = Dataset.from_list(eval_data)

    # ── Tokenizer ──
    tokenizer = AutoTokenizer.from_pretrained(
        model_path, trust_remote_code=True, padding_side="right",
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # ── Model kwargs ──
    model_kwargs = {
        "trust_remote_code": True,
        "torch_dtype": torch.bfloat16,
        "attn_implementation": "flash_attention_2",
    }

    # ── LoRA / Full SFT ──
    peft_config = None
    if not args.full_sft:
        from peft import LoraConfig
        preset = LORA_PRESETS[args.preset]
        init_weights = preset.get("init_weights", True)
        # DoRA + PiSSA may conflict in some PEFT versions; fall back to DoRA-only
        if preset["use_dora"] and isinstance(init_weights, str) and "pissa" in init_weights:
            try:
                peft_config = LoraConfig(
                    r=preset["r"],
                    lora_alpha=preset["alpha"],
                    lora_dropout=0.05,
                    target_modules=preset["target_modules"],
                    use_dora=preset["use_dora"],
                    use_rslora=preset["use_rslora"],
                    init_lora_weights=init_weights,
                    task_type="CAUSAL_LM",
                    bias="none",
                )
                print(f"  PiSSA init: {init_weights} (with DoRA)")
            except Exception:
                print(f"  PiSSA+DoRA conflict, falling back to DoRA only")
                init_weights = True
                peft_config = LoraConfig(
                    r=preset["r"],
                    lora_alpha=preset["alpha"],
                    lora_dropout=0.05,
                    target_modules=preset["target_modules"],
                    use_dora=preset["use_dora"],
                    use_rslora=preset["use_rslora"],
                    init_lora_weights=init_weights,
                    task_type="CAUSAL_LM",
                    bias="none",
                )
        else:
            peft_config = LoraConfig(
                r=preset["r"],
                lora_alpha=preset["alpha"],
                lora_dropout=0.05,
                target_modules=preset["target_modules"],
                use_dora=preset["use_dora"],
                use_rslora=preset["use_rslora"],
                init_lora_weights=init_weights,
                task_type="CAUSAL_LM",
                bias="none",
            )
        lr = args.lr or preset["lr"]
        batch_size = args.batch_size or preset["batch_size"]
        grad_accum = args.grad_accum or preset["grad_accum"]
        run_suffix = f"lora-{args.preset}-r{preset['r']}"

        print(f"\n=== LoRA Config: {args.preset} ===")
        print(f"  rank={preset['r']}, alpha={preset['alpha']}")
        print(f"  targets={preset['target_modules']}")
        print(f"  DoRA={preset['use_dora']}, rsLoRA={preset['use_rslora']}")
        print(f"  lr={lr}, batch={batch_size}, grad_accum={grad_accum}")
        effective_batch = batch_size * grad_accum
        print(f"  effective_batch_size={effective_batch}")
    else:
        lr = args.lr or 2e-5
        batch_size = args.batch_size or 1
        grad_accum = args.grad_accum or 4
        run_suffix = "full-sft"
        print("\n=== Full SFT Mode ===")

    num_epochs = 1 if args.curriculum else args.epochs
    output_dir = Path(args.output_dir) / run_suffix

    # ── Training config ──
    training_args = SFTConfig(
        output_dir=str(output_dir),
        num_train_epochs=num_epochs,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        learning_rate=lr,
        lr_scheduler_type="cosine",
        warmup_steps=100,
        weight_decay=0.01,
        max_grad_norm=1.0,
        max_length=args.max_seq_len,
        bf16=True,
        logging_steps=10,
        save_steps=args.save_steps,
        eval_steps=args.eval_steps,
        eval_strategy="steps",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        gradient_checkpointing=True,
        report_to="wandb",
        run_name=f"pentestai-{run_suffix}",
        neftune_noise_alpha=5.0,
        packing=False,
        remove_unused_columns=False,
        dataloader_num_workers=4,
        dataloader_pin_memory=True,
    )

    # ── Load model ──
    print(f"\nLoading model from {model_path}...")
    model = AutoModelForCausalLM.from_pretrained(model_path, **model_kwargs)

    # ── Trainer ──
    trainer_kwargs = dict(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        processing_class=tokenizer,
        peft_config=peft_config,
    )
    if not use_pretokenized:
        def formatting_func(example):
            return tokenizer.apply_chat_template(example["messages"], tokenize=False)
        trainer_kwargs["formatting_func"] = formatting_func

    trainer = SFTTrainer(**trainer_kwargs)

    if peft_config:
        trainable = sum(p.numel() for p in trainer.model.parameters() if p.requires_grad)
        total = sum(p.numel() for p in trainer.model.parameters())
        print(f"\nTrainable: {trainable:,} / {total:,} ({trainable/total*100:.2f}%)")

    # ── Train ──
    print("\nStarting training...")
    trainer.train()

    # ── Save ──
    final_dir = str(output_dir / "final")
    print(f"Saving to {final_dir}")
    trainer.save_model(final_dir)
    tokenizer.save_pretrained(final_dir)
    print("Done!")


if __name__ == "__main__":
    main()
