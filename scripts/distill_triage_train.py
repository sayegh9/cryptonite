"""A7.4: QLoRA fine-tune of a local triage model (Unsloth) on rule-labelled triage data.

Runs on the host GPU (no NVIDIA container runtime on this workstation), in its own venv:

    ~/cryptonite-train/.venv/bin/python scripts/distill_triage_train.py \\
        --base ~/models/hf/google/gemma-4-12B-it --data ~/cryptonite-train/data/train.jsonl \\
        --out ~/cryptonite-train/out/triage-lora

The base loads in 4-bit, LoRA adapters train on the assistant turn only (the TriageDecision JSON),
and only the adapter is saved (a few hundred MB). Certification is a separate step
(``distill_triage_eval.py``): the student is used only if it beats its own base model on held-out data.
"""

import argparse
import json
from pathlib import Path

MARKERS = [  # (user turn, model turn) per chat-template family
    ("<|turn>user\n", "<|turn>model\n"),  # Gemma 4
    ("<start_of_turn>user\n", "<start_of_turn>model\n"),  # Gemma 2/3
    ("<|im_start|>user\n", "<|im_start|>assistant\n"),  # ChatML (Qwen)
]


def response_markers(sample: str) -> tuple[str, str]:
    """Loss must cover only the answer; an unknown template would silently train on the prompt."""
    for user, model in MARKERS:
        if user in sample and model in sample:
            return user, model
    raise SystemExit(f"unknown chat template; first 300 chars: {sample[:300]!r}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--epochs", type=float, default=2.0)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--rank", type=int, default=16)
    p.add_argument("--max-seq", type=int, default=1024)
    p.add_argument("--seed", type=int, default=3407)
    args = p.parse_args()

    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastModel  # imported first: it patches transformers/trl
    from unsloth.chat_templates import train_on_responses_only

    model, tokenizer = FastModel.from_pretrained(
        model_name=str(Path(args.base).expanduser()), max_seq_length=args.max_seq, load_in_4bit=True,
        full_finetuning=False)
    model = FastModel.get_peft_model(
        model, finetune_vision_layers=False, finetune_language_layers=True, finetune_attention_modules=True,
        finetune_mlp_modules=True, r=args.rank, lora_alpha=args.rank, lora_dropout=0, bias="none",
        random_state=args.seed)

    rows = [json.loads(line) for line in open(Path(args.data).expanduser()) if line.strip()]
    text_tok = getattr(tokenizer, "tokenizer", tokenizer)  # multimodal processors wrap the text tokenizer
    texts = [text_tok.apply_chat_template(r["messages"], tokenize=False) for r in rows]
    dataset = Dataset.from_dict({"text": texts})

    trainer = SFTTrainer(
        model=model, tokenizer=tokenizer, train_dataset=dataset,
        args=SFTConfig(dataset_text_field="text", per_device_train_batch_size=2, gradient_accumulation_steps=4,
                       num_train_epochs=args.epochs, learning_rate=args.lr, warmup_ratio=0.05,
                       lr_scheduler_type="cosine", logging_steps=10, optim="adamw_8bit", weight_decay=0.01,
                       seed=args.seed, bf16=True, max_seq_length=args.max_seq, report_to="none",
                       output_dir=str(Path(args.out).expanduser()) + "-ckpt", save_strategy="no"))
    # learn the answer, not the brief: loss only on the assistant turn
    user_marker, model_marker = response_markers(texts[0])
    trainer = train_on_responses_only(trainer, instruction_part=user_marker, response_part=model_marker)
    stats = trainer.train()
    out = Path(args.out).expanduser()
    model.save_pretrained(out)
    tokenizer.save_pretrained(out)
    (out / "train_report.json").write_text(json.dumps({
        "base": args.base, "examples": len(rows), "epochs": args.epochs, "lr": args.lr, "rank": args.rank,
        "train_loss": stats.training_loss, "runtime_s": stats.metrics.get("train_runtime")}, indent=1))
    print(json.dumps({"saved": str(out), "train_loss": stats.training_loss}))


if __name__ == "__main__":
    main()
