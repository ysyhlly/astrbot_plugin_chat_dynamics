"""Controlled concurrency benchmark; no network or real model calls."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
ProviderBudget = importlib.import_module(f"{ROOT.name}.core.provider_budget").ProviderBudget


def summarize(values):
    ordered = sorted(values)
    # Linear interpolation, matching the explicitly recorded quantile method.
    def quantile(q):
        position = (len(ordered)-1)*q
        lower = int(position)
        upper = min(lower+1, len(ordered)-1)
        return ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)
    return {"p50_seconds": quantile(.5), "p95_seconds": quantile(.95),
            "mean_seconds": statistics.mean(values), "samples": len(values)}


async def round_trip(reserved, delay):
    semaphore = asyncio.Semaphore(2)
    budget = ProviderBudget(capacity=2)
    waits = {"reply": [], "background": []}
    counts = {"reply": 0, "background": 0}
    start = time.perf_counter()

    async def invoke(purpose):
        queued = time.perf_counter()
        async def provider():
            waits[purpose].append(time.perf_counter()-queued)
            counts[purpose] += 1
            await asyncio.sleep(delay)
        if reserved:
            await budget.run("controlled-provider", "title" if purpose == "background" else "reply", provider)
        else:
            async with semaphore:
                await provider()

    background = [asyncio.create_task(invoke("background")) for _ in range(4)]
    # Both cases enqueue the same four background calls before the same reply.
    await asyncio.sleep(delay/6)
    reply = asyncio.create_task(invoke("reply"))
    await asyncio.gather(*background, reply)
    return {"queue_seconds": waits, "total_seconds": time.perf_counter()-start,
            "calls": counts}


async def benchmark(rounds, warmup, delay):
    cases = {"fifo_semaphore": [], "reserved_provider_budget": []}
    for iteration in range(rounds+warmup):
        # Alternate execution order to reduce systematic ordering effects.
        order = list(cases) if iteration % 2 == 0 else list(reversed(cases))
        for name in order:
            record = await round_trip(name == "reserved_provider_budget", delay)
            if iteration >= warmup:
                cases[name].append(record)
    result = {
        "kind": "controlled synthetic provider delay; not production performance",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "parameters": {"measured_rounds_per_case": rounds, "warmup_rounds_per_case": warmup,
                       "provider_delay_seconds": delay, "capacity": 2, "reserved_background_capacity": 1,
                       "background_calls_per_round": 4, "reply_calls_per_round": 1,
                       "reply_arrival_delay_seconds": delay/6,
                       "quantile": "linear interpolation at (n-1)*q"},
        "cases": {},
        "interpretation": "Reply reservation trades background queue latency and overall completion time for reply responsiveness. No production benefit is inferred.",
    }
    for name, records in cases.items():
        result["cases"][name] = {
            "reply_queue": summarize([v for r in records for v in r["queue_seconds"]["reply"]]),
            "background_queue": summarize([v for r in records for v in r["queue_seconds"]["background"]]),
            "total_duration": summarize([r["total_seconds"] for r in records]),
            "rounds": records,
        }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--delay", type=float, default=.06)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/provider-budget-benchmark.json")
    args = parser.parse_args()
    if args.rounds < 10 or args.warmup < 1 or args.delay <= 0:
        parser.error("Require rounds >= 10, warmup >= 1 and delay > 0")
    output = asyncio.run(benchmark(args.rounds, args.warmup, args.delay))
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    print(json.dumps({name: {k: v for k, v in case.items() if k != "rounds"}
                      for name, case in output["cases"].items()}, indent=2))
