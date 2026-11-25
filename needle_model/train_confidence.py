"""Train ConfidenceHead locally on merged (base + LoRA) Needle 3 weights and export calibrated .cact."""
from __future__ import annotations

import argparse
import json
import time
import numpy as np
import jax
import jax.numpy as jnp
import optax

from cactus.agent import fetch
from cactus.model.architecture import ConfidenceHead, SimpleAttentionNetwork, effective_kv_window
from cactus.model.checkpoints import read_adapter
from cactus.model.export import read_tokenizer_blob, write_export
from cactus.model.finetune import merge_lora, render_example, rung
from cactus.model.quantize import WEIGHT_BITS, configure_deploy, cq_ste_params
from cactus.model.run import load_checkpoint
from cactus.model.tokenizer import BOS_ID, EOS_ID, PAD_ID, get_tokenizer


def encode_conf_examples(path, tokenizer, max_len=1536):
    seqs, labels, prefix_lens = [], [], []
    for line in open(path, encoding="utf-8"):
        ex = json.loads(line)
        prompt, target = render_example(ex)
        p_ids = [BOS_ID] + tokenizer.encode(prompt)
        t_ids = tokenizer.encode(target) + [EOS_ID]
        ids = (p_ids + t_ids)[:max_len]
        pad = max_len - len(ids)
        seqs.append(ids + [PAD_ID] * pad)
        labels.append(float(ex["label"]))
        prefix_lens.append(len(p_ids))
    return np.asarray(seqs, np.int32), np.asarray(labels, np.float32), np.asarray(prefix_lens, np.int32)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint", default="/mnt/c/Users/ACER/servo-models/needle-tune/checkpoints/needle3.safetensors")
    p.add_argument("--lora", default="/mnt/c/Users/ACER/servo-models/needle-tune/checkpoints/needle_lora_v2.safetensors")
    p.add_argument("--data", default="needle_model/conf_train.jsonl")
    p.add_argument("--out", default="/mnt/c/Users/ACER/servo-models/needle-tune/needle_tuned.cact")
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--lr", type=float, default=5e-4)
    p.add_argument("--max-len", type=int, default=1536)
    args = p.parse_args()

    t0 = time.time()
    params, config, _ = load_checkpoint(args.checkpoint, return_run=True)
    config.dtype = "float32"
    params = jax.tree.map(lambda a: np.asarray(a).astype(np.float32), params)
    adapter = read_adapter(args.lora)
    lora = {tuple(key.split("/")): {"A": jnp.asarray(v["A"]), "B": jnp.asarray(v["B"])}
            for key, v in adapter["lora"].items()}
    merged = merge_lora(params, lora, adapter["scale"])
    configure_deploy(act_bits=getattr(config, "act_bits", 8), kv_bits=getattr(config, "kv_bits", 8))

    model = SimpleAttentionNetwork(config)
    tokenizer = get_tokenizer(config.vocab_size)
    seqs, labels, prefix_lens = encode_conf_examples(args.data, tokenizer, args.max_len)
    print(f"Loaded {len(seqs)} calibration examples (pos={int(labels.sum())}, neg={int(len(labels) - labels.sum())})", flush=True)

    rng = np.random.default_rng(42)
    perm = rng.permutation(len(seqs))
    n_val = max(50, int(len(seqs) * 0.1))
    val_idx, tr_idx = perm[:n_val], perm[n_val:]

    # Quantize backbone weights once (frozen during probe training)
    backbone_params = cq_ste_params(merged, WEIGHT_BITS)
    head_params = jax.device_put(merged[ConfidenceHead.key])
    window = effective_kv_window(config)

    @jax.jit
    def extract_cells(ids, p_len):
        pos = jnp.arange(ids.shape[1])[None, :]
        sink = pos < (p_len[:, None] - 16)
        return model.apply({"params": backbone_params}, ids, quant=True, window=window, sink=sink, method=model._head_cells)

    print("Extracting frozen hidden cells on GPU (1 forward pass per example)...", flush=True)
    # Extract cells once; keep only pooled-ready cells in CPU RAM? Wait: cells are (1, 1536, 21, 768) float16 ~ 48MB each -> too big for RAM!
    # Instead: train head directly on GPU with batch=1 using stop_gradient (backbone forward runs in ~0.2s/step, zero backbone grads!).
    total_steps = args.epochs * len(tr_idx)
    schedule = optax.warmup_cosine_decay_schedule(0.0, args.lr, max(10, total_steps // 10), total_steps)
    opt = optax.chain(optax.clip_by_global_norm(1.0), optax.adamw(schedule, weight_decay=1e-2))
    opt_state = opt.init(head_params)

    # Class weight so pos and neg are balanced in BCE
    n_pos = max(1.0, float(labels[tr_idx].sum()))
    n_neg = max(1.0, float(len(tr_idx) - n_pos))
    pos_weight = n_neg / n_pos

    def head_loss(hp, ids, p_len, target):
        p = {**backbone_params, ConfidenceHead.key: hp}
        pos = jnp.arange(ids.shape[1])[None, :]
        sink = pos < (p_len[:, None] - 16)
        logits = model.apply({"params": p}, ids, quant=True, window=window, sink=sink, method=model.forward_confidence)
        bce = optax.sigmoid_binary_cross_entropy(logits, target)
        w = jnp.where(target > 0.5, pos_weight, 1.0)
        return (bce * w).mean(), logits[0]

    @jax.jit
    def train_step(hp, os_, ids, p_len, target):
        (loss, logit), grads = jax.value_and_grad(head_loss, has_aux=True)(hp, ids, p_len, target)
        updates, os_ = opt.update(grads, os_, hp)
        return optax.apply_updates(hp, updates), os_, loss, logit

    @jax.jit
    def eval_step(hp, ids, p_len, target):
        loss, logit = head_loss(hp, ids, p_len, target)
        return loss, jax.nn.sigmoid(logit)

    best_val_acc = -1.0
    best_hp = head_params
    for ep in range(args.epochs):
        order = rng.permutation(tr_idx)
        losses = []
        for idx in order:
            hp_ids = jnp.asarray(seqs[idx:idx + 1])
            hp_plen = jnp.asarray(prefix_lens[idx:idx + 1])
            hp_y = jnp.asarray(labels[idx:idx + 1])
            head_params, opt_state, loss, _ = train_step(head_params, opt_state, hp_ids, hp_plen, hp_y)
            losses.append(float(loss))
        val_probs, val_losses = [], []
        for idx in val_idx:
            vl, vp = eval_step(head_params, jnp.asarray(seqs[idx:idx + 1]), jnp.asarray(prefix_lens[idx:idx + 1]), jnp.asarray(labels[idx:idx + 1]))
            val_losses.append(float(vl))
            val_probs.append(float(vp))
        val_preds = np.asarray(val_probs) >= 0.5
        val_acc = float((val_preds == (labels[val_idx] >= 0.5)).mean())
        print(f"  epoch {ep + 1}/{args.epochs}  train_loss={np.mean(losses):.4f}  val_loss={np.mean(val_losses):.4f}  val_acc@0.5={val_acc:.3f}", flush=True)
        if val_acc >= best_val_acc:
            best_val_acc = val_acc
            best_hp = jax.tree.map(np.asarray, head_params)

    # Export calibrated archive
    merged[ConfidenceHead.key] = best_hp
    merged, config = rung(merged, config, None)
    base_archive = fetch.fetch_weights(3, force=False)
    info = write_export(merged, config, args.out, bits=WEIGHT_BITS,
                        tokenizer=read_tokenizer_blob(base_archive),
                        kv_window=effective_kv_window(config))
    print(f"Exported calibrated archive {args.out} ({info['bytes']/1e6:.2f} MB, {info['tensors']} tensors) in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
