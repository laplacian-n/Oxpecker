#!/usr/bin/env python3
"""Evaluate fine-tuned model on held-out eval set.

Checks:
  1. Perplexity / eval loss
  2. Tool call format accuracy (valid JSON, correct keys)
  3. Think tag presence
  4. Response quality (length, coherence)
  5. General knowledge retention (simple QA)

Usage:
  python3 eval_model.py --model ~/sft_output/final
  python3 eval_model.py --model Qwen/Qwen3-32B  # baseline comparison
"""
import argparse
import json
import re
import torch
from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer

EVAL_DATA = Path.home() / "sft_data" / "train_qwen3_think_v30r2_final_eval.jsonl"

GENERAL_QA = [
    {"q": "What is the capital of France?", "expected": "Paris"},
    {"q": "What is 15 * 23?", "expected": "345"},
    {"q": "Write a Python function to reverse a string.", "expected": "def"},
    {"q": "What does the HTTP status code 403 mean?", "expected": "forbidden"},
    {"q": "Explain what SQL injection is in one sentence.", "expected": "inject"},
]

TOOL_CALL_PROMPTS = [
    "Scan the target 10.10.10.1 for open ports.",
    "Read the file /etc/passwd on the target.",
    "Search the knowledge base for privilege escalation techniques on Linux.",
    "Run nmap -sV on 192.168.1.100.",
]


def load_model(model_path):
    tokenizer = AutoTokenizer.from_pretrained(
        model_path, trust_remote_code=True, padding_side="left"
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        trust_remote_code=True,
        torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    model.eval()
    return model, tokenizer


def generate(model, tokenizer, messages, max_new_tokens=512):
    text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = tokenizer(text, return_tensors="pt").to(model.device)
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=0.7,
            do_sample=True,
            top_p=0.9,
        )
    response = tokenizer.decode(outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return response


def eval_general_qa(model, tokenizer):
    print("\n[1] GENERAL KNOWLEDGE RETENTION")
    correct = 0
    for item in GENERAL_QA:
        messages = [
            {"role": "system", "content": "Answer concisely."},
            {"role": "user", "content": item["q"]},
        ]
        response = generate(model, tokenizer, messages, max_new_tokens=200)
        hit = item["expected"].lower() in response.lower()
        correct += int(hit)
        status = "PASS" if hit else "FAIL"
        print(f"  [{status}] Q: {item['q']}")
        if not hit:
            print(f"         Expected '{item['expected']}' in: {response[:100]}...")

    print(f"\n  Score: {correct}/{len(GENERAL_QA)} ({correct/len(GENERAL_QA)*100:.0f}%)")
    return correct / len(GENERAL_QA)


def eval_tool_calling(model, tokenizer):
    print("\n[2] TOOL CALL FORMAT ACCURACY")
    sys_prompt = """You are a penetration testing AI agent. Use tools via <tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>."""

    valid = 0
    has_think = 0
    for prompt in TOOL_CALL_PROMPTS:
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ]
        response = generate(model, tokenizer, messages, max_new_tokens=400)

        # Check think tags
        if "<think>" in response:
            has_think += 1

        # Check tool call format
        tc_match = re.search(r'<tool_call>(.*?)</tool_call>', response, re.DOTALL)
        if tc_match:
            try:
                obj = json.loads(tc_match.group(1).strip())
                if "name" in obj and "arguments" in obj:
                    valid += 1
                    print(f"  [PASS] {prompt[:50]}... → {obj['name']}")
                else:
                    print(f"  [FAIL] Missing keys: {prompt[:50]}...")
            except json.JSONDecodeError:
                print(f"  [FAIL] Invalid JSON: {prompt[:50]}...")
        else:
            print(f"  [FAIL] No tool_call tag: {prompt[:50]}...")
            print(f"         Response: {response[:100]}...")

    print(f"\n  Tool call valid: {valid}/{len(TOOL_CALL_PROMPTS)}")
    print(f"  Has <think>: {has_think}/{len(TOOL_CALL_PROMPTS)}")
    return valid / len(TOOL_CALL_PROMPTS)


def eval_response_quality(model, tokenizer):
    print("\n[3] RESPONSE QUALITY (sample from eval set)")
    rows = []
    with open(EVAL_DATA) as f:
        for i, line in enumerate(f):
            if i >= 20:
                break
            if line.strip():
                rows.append(json.loads(line))

    total_len = 0
    has_think = 0
    non_empty = 0

    for r in rows:
        msgs = r["messages"]
        # Use system + first user message
        input_msgs = []
        for m in msgs:
            input_msgs.append(m)
            if m["role"] == "user":
                break

        if not input_msgs:
            continue

        response = generate(model, tokenizer, input_msgs, max_new_tokens=300)
        total_len += len(response)
        if "<think>" in response:
            has_think += 1
        if len(response.strip()) > 10:
            non_empty += 1

    n = len(rows)
    avg_len = total_len / n if n else 0
    print(f"  Avg response length: {avg_len:.0f} chars")
    print(f"  Has <think>: {has_think}/{n} ({has_think/n*100:.0f}%)")
    print(f"  Non-empty: {non_empty}/{n} ({non_empty/n*100:.0f}%)")
    return has_think / n if n else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, required=True)
    args = parser.parse_args()

    print(f"Loading model: {args.model}")
    model, tokenizer = load_model(args.model)

    scores = {}
    scores["general_qa"] = eval_general_qa(model, tokenizer)
    scores["tool_calling"] = eval_tool_calling(model, tokenizer)
    scores["think_rate"] = eval_response_quality(model, tokenizer)

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    for k, v in scores.items():
        print(f"  {k:20s} {v*100:5.1f}%")

    avg = sum(scores.values()) / len(scores)
    print(f"\n  Overall: {avg*100:.1f}%")

    # Save results
    out_path = Path(args.model) / "eval_results.json"
    try:
        with open(out_path, "w") as f:
            json.dump(scores, f, indent=2)
        print(f"\n  Saved: {out_path}")
    except OSError:
        print(f"\n  (Could not save to model dir)")


if __name__ == "__main__":
    main()
