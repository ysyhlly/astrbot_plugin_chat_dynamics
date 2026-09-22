"""Supervised distillation against upstream Laya's differentiable typed scorer."""

import argparse
from collections import Counter, defaultdict
import hashlib
import math
from pathlib import Path
import random
import shutil

from .backend import LayaBackend, UPSTREAM_REVISION, WEIGHTS_REVISION, canonical, task_name


def label_index(row):
    q, label = row["candidates"], row["teacher_label"]
    kind = q["type"]
    value = label[kind] if isinstance(label, dict) else label
    if kind == "choice":
        return list(q["criteria"]).index(value)
    if kind == "noul":
        if value not in (True, False, 0, 1):
            raise ValueError("noul training labels must be hard binary labels")
        return int(value)
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= len(q["criteria"]) - 1:
        raise ValueError("invalid score label")
    return value


def supervised_loss(logits, kind, label):
    import torch
    import torch.nn.functional as F

    if kind == "noul":
        return F.binary_cross_entropy_with_logits(logits[1] - logits[0], logits.new_tensor(float(label)))
    if kind == "choice":
        return F.cross_entropy(logits.unsqueeze(0), torch.tensor([int(label)], device=logits.device))
    # Adjacent ordinal target, with cumulative distribution loss preserving distance.
    low, high = math.floor(label), math.ceil(label)
    target = torch.zeros_like(logits)
    target[low] = 1.0 - (label - low)
    if high != low:
        target[high] = label - low
    return (
        -(target * F.log_softmax(logits, -1)).sum() + ((logits.softmax(-1).cumsum(-1) - target.cumsum(-1)) ** 2).mean()
    )


def fit_temperature(logits, labels):
    """Deterministic bounded NLL fit; input must be calibration split only."""
    import numpy as np

    z = np.asarray(logits, dtype=float)
    y = np.asarray(labels, dtype=int)
    if not len(y) or not np.isfinite(z).all():
        raise ValueError("invalid calibration data")
    best = (float("inf"), 1.0)
    for temperature in np.geomspace(0.25, 8.0, 100):
        scaled = z / temperature
        scaled -= scaled.max(axis=1, keepdims=True)
        p = np.exp(scaled)
        p /= p.sum(axis=1, keepdims=True)
        loss = -np.log(np.maximum(p[np.arange(len(y)), y], 1e-12)).mean()
        best = min(best, (float(loss), float(temperature)))
    return best[1]


def wilson_upper(errors, count, z=1.96):
    if not count:
        return 1.0
    p = errors / count
    denominator = 1 + z * z / count
    return (p + z * z / (2 * count) + z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count))) / denominator


def grouped_threshold(confidence, correct, request_keys):
    """Any error rejects a request; repeated candidates cannot inflate evidence."""
    groups = {}
    for conf, good, key in zip(confidence, correct, request_keys):
        if key is None:
            continue
        old_conf, old_good = groups.get(key, (1.0, True))
        groups[key] = (min(old_conf, float(conf)), old_good and bool(good))
    result = dict(
        threshold=1.01,
        effective_groups=len(groups),
        accepted_groups=0,
        accepted_group_errors=0,
        accepted_error_upper=1.0,
    )
    for candidate in sorted({conf for conf, _ in groups.values()}):
        accepted = [good for conf, good in groups.values() if conf >= candidate]
        errors = sum(not good for good in accepted)
        upper = wilson_upper(errors, len(accepted))
        if len(accepted) >= 20 and upper <= 0.05:
            result.update(
                threshold=candidate,
                accepted_groups=len(accepted),
                accepted_group_errors=errors,
                accepted_error_upper=upper,
            )
            break
    return result


def balanced_epoch_indices(rows, *, seed, max_repeats=3):
    """Weighted without-replacement tickets cap each row at max_repeats per epoch."""
    if max_repeats < 1:
        raise ValueError("max_repeats must be positive")
    counts = Counter((row["task_id"], round(label_index(row))) for row in rows)
    rng = random.Random(seed)
    tickets = []
    for index, row in enumerate(rows):
        inverse_weight = counts[(row["task_id"], round(label_index(row)))]
        for _ in range(max_repeats):
            tickets.append((-math.log(max(rng.random(), 1e-15)) * inverse_weight, index))
    selected = [index for _, index in sorted(tickets)[: len(rows)]]
    rng.shuffle(selected)
    return selected


class EarlyStopping:
    def __init__(self, patience=2, min_delta=1e-4):
        if patience < 1 or min_delta < 0:
            raise ValueError("invalid early stopping configuration")
        self.patience, self.min_delta = patience, min_delta
        self.best_loss, self.best_epoch, self.stale = float("inf"), 0, 0
        self.patience_reference = float("inf")

    def observe(self, loss, epoch):
        if not math.isfinite(loss):
            raise ValueError("non-finite validation loss")
        improved = loss < self.best_loss
        if improved:
            self.best_loss, self.best_epoch = loss, epoch
        if loss < self.patience_reference - self.min_delta:
            self.patience_reference, self.stale = loss, 0
        else:
            self.stale += 1
        return improved, self.stale >= self.patience


def calibrate(backend, rows, *, cancel=lambda: False):
    import numpy as np

    buckets = defaultdict(list)
    for row in rows:
        if cancel():
            raise InterruptedError("calibration cancelled")
        qid = row["task_id"]
        if backend.prepare(row["state"], {qid: row["candidates"]}) != row["state"]:
            raise ValueError("dataset must contain tokenizer-prepared snapshots")
        logits = backend.logits(row["state"], {qid: row["candidates"]})[0]
        request_id = row.get("metadata", {}).get("request_id")
        key = (row["session_id"], request_id) if isinstance(request_id, str) and request_id else None
        buckets[f"{task_name(qid)}:{len(logits)}"].append((logits, round(label_index(row)), key))
    result = {}
    for bucket, pairs in buckets.items():
        logits, labels, keys = zip(*pairs)
        temperature = fit_temperature(logits, labels)
        z = np.asarray(logits) / temperature
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        confidence, correct = p.max(axis=1), p.argmax(axis=1) == labels
        result[bucket] = {
            "temperature": temperature,
            "samples": len(pairs),
            "unknown_request_samples": sum(key is None for key in keys),
            **grouped_threshold(confidence, correct, keys),
        }
    return result


def train(
    base,
    rows,
    output,
    *,
    epochs=3,
    seed=42,
    learning_rate=2e-5,
    accumulation=16,
    validation_rows=None,
    patience=2,
    min_delta=1e-4,
    max_repeats=3,
    cancel=lambda: False,
    progress=lambda **kw: None,
):
    import torch
    from laya.common import build_sequence, collate_items, QTYPES
    from safetensors.torch import save_file

    output = Path(output)
    if output.exists():
        raise FileExistsError("model output is immutable; select a new model id")
    if not validation_rows:
        raise ValueError("independent training validation rows are required")
    if epochs < 1:
        raise ValueError("epochs must be positive")
    random.seed(seed)
    torch.manual_seed(seed)
    backend = LayaBackend(base)
    agent = backend.agent
    for parameter in agent.model.act_head.parameters():
        parameter.requires_grad_(False)
    if hasattr(agent.model.encoder, "gradient_checkpointing_enable"):
        agent.model.encoder.gradient_checkpointing_enable()
    items = []
    for row in [*rows, *validation_rows]:
        label = label_index(row)
        if backend.prepare(row["state"], {row["task_id"]: row["candidates"]}) != row["state"]:
            raise ValueError("dataset must contain tokenizer-prepared snapshots")
        internal = agent._to_internal(row["candidates"])
        seq, markers = build_sequence(
            agent.tok, row["state"], internal, agent.cfg.get("max_len", 1024), agent.cfg.get("head_max_len", 192)
        )
        expected = 2 if internal["t"] == "noul" else len(internal["crit"])
        if len(markers) != expected:
            raise ValueError("training options were truncated")
        items.append(
            {"ids": seq, "markers": markers, "qtype": QTYPES[internal["t"]], "label": label, "kind": internal["t"]}
        )
    validation_items = items[len(rows) :]
    items = items[: len(rows)]
    if not items:
        raise ValueError("empty training dataset")
    optimizer = torch.optim.AdamW((p for p in agent.model.parameters() if p.requires_grad), lr=learning_rate)
    # Allocate Adam's two moments before probing, with a zero learning rate so
    # checkpoint weights remain unchanged. Probe includes actual optimizer VRAM.
    for group in optimizer.param_groups:
        group["lr"] = 0.0
        for parameter in group["params"]:
            parameter.grad = torch.zeros_like(parameter)
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    for parameter_state in optimizer.state.values():
        parameter_state["step"].zero_()
    for group in optimizer.param_groups:
        group["lr"] = learning_rate

    def forward(selected):
        batch = collate_items([[item] for item in selected], agent.tok.pad_token_id)
        with torch.autocast(device_type=agent.device.type, dtype=agent.dtype, enabled=agent.device.type == "cuda"):
            logits, _ = agent.model(
                *(
                    batch[k].to(agent.device)
                    for k in ("input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype")
                )
            )
            return torch.stack(
                [
                    supervised_loss(logits[i, : len(item["markers"])].float(), item["kind"], item["label"])
                    for i, item in enumerate(selected)
                ]
            ).mean()

    agent.model.train()
    batch_size = 1
    # Probe worst-length examples; optimizer states are allocated separately below.
    probe = sorted(items, key=lambda x: len(x["ids"]), reverse=True)
    for candidate in (1, 2, 4):
        try:
            forward((probe * candidate)[:candidate]).backward()
            optimizer.zero_grad(set_to_none=True)
            batch_size = candidate
        except torch.cuda.OutOfMemoryError:
            optimizer.zero_grad(set_to_none=True)
            torch.cuda.empty_cache()
            if candidate == 1:
                raise
            break
    scaler = torch.amp.GradScaler("cuda", enabled=agent.device.type == "cuda" and agent.dtype == torch.float16)
    step = 0
    stopper = EarlyStopping(patience, min_delta)
    best_weights = None
    validation_history = []
    for epoch in range(epochs):
        agent.model.train()
        indices = balanced_epoch_indices(rows, seed=seed + epoch, max_repeats=max_repeats)
        batches = [indices[i : i + batch_size] for i in range(0, len(indices), batch_size)]
        for index, indices_batch in enumerate(batches):
            if cancel():
                raise InterruptedError("training cancelled")
            window = min(accumulation, len(batches) - (index // accumulation) * accumulation)
            loss = forward([items[i] for i in indices_batch])
            scaler.scale(loss / window).backward()
            if (index + 1) % accumulation == 0 or index + 1 == len(batches):
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(agent.model.parameters(), 1.0)
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
            step += 1
            progress(
                epoch=epoch + 1,
                step=step,
                loss=float(loss.detach()),
                batch_size=batch_size,
                device=str(agent.device),
                peak_cuda_memory_bytes=torch.cuda.max_memory_allocated() if agent.device.type == "cuda" else 0,
            )
        agent.model.eval()
        losses = []
        with torch.inference_mode():
            for item in validation_items:
                if cancel():
                    raise InterruptedError("validation cancelled")
                losses.append(float(forward([item])))
        validation_loss = sum(losses) / len(losses)
        improved, stop = stopper.observe(validation_loss, epoch + 1)
        validation_history.append({"epoch": epoch + 1, "loss": validation_loss})
        if improved:
            best_weights = {
                k: v.detach().to(device="cpu", copy=True).contiguous() for k, v in agent.model.state_dict().items()
            }
        progress(
            epoch=epoch + 1,
            step=step,
            validation_loss=validation_loss,
            best_epoch=stopper.best_epoch,
            early_stopped=stop,
            batch_size=batch_size,
            device=str(agent.device),
            peak_cuda_memory_bytes=torch.cuda.max_memory_allocated() if agent.device.type == "cuda" else 0,
        )
        if stop:
            break
    output.mkdir(parents=True)
    try:
        shutil.copytree(Path(base) / "encoder", output / "encoder")
        agent.tok.save_pretrained(output / "tokenizer")
        cfg = dict(agent.cfg, temperature=[1.0, 1.0, 1.0], temperature_by_options={})
        (output / "rl_agent_config.json").write_text(canonical(cfg), encoding="utf-8")
        save_file(
            best_weights,
            str(output / "model.safetensors"),
        )
        (output / "training.json").write_text(
            canonical(
                {
                    "seed": seed,
                    "device": str(agent.device),
                    "batch_size": batch_size,
                    "peak_cuda_memory_bytes": torch.cuda.max_memory_allocated() if agent.device.type == "cuda" else 0,
                    "epochs": epochs,
                    "epochs_completed": len(validation_history),
                    "best_epoch": stopper.best_epoch,
                    "best_validation_loss": stopper.best_loss,
                    "validation_history": validation_history,
                    "early_stopping_patience": patience,
                    "early_stopping_min_delta": min_delta,
                    "max_repeats_per_epoch": max_repeats,
                    "training_samples": len(rows),
                    "validation_samples": len(validation_rows),
                    "validation_fingerprint": hashlib.sha256(canonical(validation_rows).encode()).hexdigest(),
                    "learning_rate": learning_rate,
                    "upstream_revision": UPSTREAM_REVISION,
                    "weights_revision": WEIGHTS_REVISION,
                    "act_head_trained": False,
                    "dataset_fingerprint": hashlib.sha256(canonical(rows).encode()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
    except BaseException:
        (output / "INCOMPLETE").touch()
        raise
    return output


def download_base(destination):
    from huggingface_hub import snapshot_download

    source = (
        Path(snapshot_download("convaiinnovations/laya", revision=WEIGHTS_REVISION, allow_patterns=["multilingual/*"]))
        / "multilingual"
    )
    shutil.copytree(source, destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["download-base", "worker"])
    parser.add_argument("--destination", default="/models/base")
    args = parser.parse_args()
    if args.command == "download-base":
        download_base(args.destination)
    else:
        from .jobs import worker

        worker()


if __name__ == "__main__":
    main()
