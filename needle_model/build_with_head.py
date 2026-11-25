"""Build needle archive merging LoRA weights while PRESERVING the base confidence head."""
import argparse
import jax.numpy as jnp
from cactus.agent import fetch
from cactus.model.finetune import merge_lora, rung
from cactus.model.architecture import ConfidenceHead, effective_kv_window
from cactus.model.checkpoints import read_adapter
from cactus.model.export import read_tokenizer_blob, write_export
from cactus.model.quantize import WEIGHT_BITS
from cactus.model.run import load_checkpoint

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint", default="/mnt/c/Users/ACER/servo-models/needle-tune/checkpoints/needle3.safetensors")
    p.add_argument("--lora", default="/mnt/c/Users/ACER/servo-models/needle-tune/checkpoints/needle_lora_v2.safetensors")
    p.add_argument("--out", default="/mnt/c/Users/ACER/servo-models/needle-tune/needle_tuned.cact")
    args = p.parse_args()

    base_archive = fetch.fetch_weights(3, force=False)
    params, config, _ = load_checkpoint(args.checkpoint, return_run=True)
    adapter = read_adapter(args.lora)
    lora = {tuple(key.split("/")): {"A": jnp.asarray(v["A"]), "B": jnp.asarray(v["B"])}
            for key, v in adapter["lora"].items()}
    params = merge_lora(params, lora, adapter["scale"])
    print(f"  merged {len(lora)} weight groups from {args.lora}")
    print(f"  confidence head kept: {ConfidenceHead.key in params}")
    params, config = rung(params, config, None)
    info = write_export(params, config, args.out, bits=WEIGHT_BITS,
                        tokenizer=read_tokenizer_blob(base_archive),
                        kv_window=effective_kv_window(config))
    print(f"  wrote {args.out} ({info['bytes']/1e6:.2f} MB, {info['tensors']} tensors)")

if __name__ == "__main__":
    main()
