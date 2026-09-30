"""Start a pinned Kev sidecar without upstream's missing-run fallback.

Run this with a Python environment containing the pinned Kev source and its
serve extra. The plugin itself never imports Kev or loads model weights.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--row-budget", type=int, default=4096)
    parser.add_argument("--base", required=True)
    parser.add_argument("--base-revision", required=True)
    parser.add_argument("--weights-sha256", required=True)
    parser.add_argument("--head-sha256", required=True)
    args = parser.parse_args()
    run = args.run.resolve(strict=True)
    adapter = run / "adapter_model.safetensors"
    head = run / "head.pt"
    if not adapter.is_file() or not head.is_file():
        parser.error("a local Kev LoRA adapter and head.pt are required")
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    if not 1024 <= args.row_budget <= 16384:
        parser.error("row budget must be in [1024, 16384]")
    from kev.checkpoint import Checkpoint

    checkpoint = Checkpoint(str(run))
    if (checkpoint.meta.base != args.base or
            checkpoint.meta.base_revision != args.base_revision):
        parser.error("base model or revision differs from the pinned release")
    if (checkpoint.weights_sha256() != args.weights_sha256 or
            sha256(head) != args.head_sha256):
        parser.error("Kev adapter or pointer head digest mismatch")
    import kev.model as kev_model

    original_rows_per_pass = kev_model.rows_per_pass
    kev_model.rows_per_pass = lambda rows, prefix_len=0: original_rows_per_pass(
        rows, prefix_len, budget=args.row_budget)
    # The upstream CLI silently selects a different run when --run is absent.
    # A verified existing run is supplied in both slots so it cannot do that.
    sys.argv = [sys.argv[0], "--run", str(run), "--fallback", str(run),
                "--port", str(args.port)]
    from kev.serve import main as serve_main

    serve_main()


if __name__ == "__main__":
    main()
