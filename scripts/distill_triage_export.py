"""A7.4: merge a certified triage adapter into its base and import it into Ollama.

Ollama 0.33 cannot load this LoRA as an ADAPTER for Gemma 4, so the adapter is merged into 16-bit
weights (Unsloth reloads the base shard by shard on the CPU; about 24 GB on disk for Gemma 4 12B) and
Ollama imports and quantizes those (``ollama create -q``). The merged folder is deleted afterwards unless
``--keep``. Certification is not repeated here: run the production evals against the Ollama model
before routing anything to it.

    HF_HUB_OFFLINE=1 ~/cryptonite-train/.venv/bin/python scripts/distill_triage_export.py \\
        --base ~/models/hf/google/gemma-4-12B-it --adapter ~/cryptonite-train/out/triage-lora \\
        --merged ~/cryptonite-train/out/triage-merged --name cryptonite-triage:gemma4-12b-q4km
"""

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--adapter", required=True)
    p.add_argument("--merged", required=True)
    p.add_argument("--name", required=True, help="Ollama model name")
    p.add_argument("--quantize", default="q4_K_M")
    p.add_argument("--keep", action="store_true", help="keep the merged 16-bit folder")
    args = p.parse_args()
    merged = Path(args.merged).expanduser()

    from unsloth import FastModel

    model, tokenizer = FastModel.from_pretrained(model_name=str(Path(args.adapter).expanduser()),
                                                 max_seq_length=1024, load_in_4bit=True)
    model.save_pretrained_merged(str(merged), tokenizer, save_method="merged_16bit")
    del model

    modelfile = merged / "Modelfile"
    modelfile.write_text(f"FROM {merged}\nPARAMETER temperature 0\n")
    env = {**os.environ, "OLLAMA_HOST": os.environ.get("OLLAMA_HOST", "172.17.0.1:11434")}
    subprocess.run(["ollama", "create", args.name, "-q", args.quantize, "-f", str(modelfile)], check=True, env=env)
    if not args.keep:
        shutil.rmtree(merged)
    print(f"imported {args.name}")


if __name__ == "__main__":
    main()
