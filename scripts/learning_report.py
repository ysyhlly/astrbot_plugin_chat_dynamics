"""Turn exported replay annotations into learning samples and a factor report.

Read-only with respect to the plugin: this script never touches live routing,
configuration, or AstrBot state. It reads annotation JSON (as exported from the
replay page) and prints what the labelled errors look like.

    python scripts/learning_report.py --annotations annotations.json --bot-id <id>
    python scripts/learning_report.py --annotations annotations.json --save samples.jsonl
    python scripts/learning_report.py --store samples.jsonl --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_chat_dynamics.core.learning import (  # noqa: E402
    LearningSample,
    analyze_recipient,
    samples_from_annotations,
    summarize,
)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_annotations(path: Path) -> list:
    payload = load_json(path)
    if isinstance(payload, dict):
        for key in ("records", "annotations", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []
    return payload if isinstance(payload, list) else []


def load_store(path: Path) -> list[LearningSample]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(LearningSample.from_dict(json.loads(line)))
        except (ValueError, TypeError):
            continue
    return rows


def render(report: dict) -> str:
    lines = [f"样本总数 {report['total']}（{report['sample_note']}）", ""]
    for task, data in report["tasks"].items():
        lines.append(f"[{task}] 准确 {data['accuracy'] * 100:.1f}%  "
                     f"({data['correct']}/{data['total']})")
        if data["error_counts"]:
            errors = "  ".join(f"{k}:{v}" for k, v in data["error_counts"].items())
            lines.append(f"    错误分布  {errors}")
        lines.append(f"    判对时平均置信 {data['confidence_when_right']:.2f}  "
                     f"判错时 {data['confidence_when_wrong']:.2f}")
    if report["factors"]:
        lines += ["", "因子差异（判错均值 - 判对均值，正数表示判错时该因子更大）"]
        for row in report["factors"][:12]:
            lines.append(f"    {row['code']:34s} {row['delta']:+.3f}  "
                         f"(n={row['support']} 错{row['wrong_n']}/对{row['right_n']})")
    else:
        lines += ["", "因子差异：样本不足，至少需要同一因子在判错与判对两侧各出现若干次"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--annotations", type=Path, help="annotation JSON exported from replay")
    parser.add_argument("--store", type=Path, help="existing sample JSONL to include")
    parser.add_argument("--save", type=Path, help="write the derived samples here")
    parser.add_argument("--bot-id", default="",
                        help="bot account id; required for recipient samples")
    parser.add_argument("--min-support", type=int, default=4)
    parser.add_argument("--recommend", action="store_true",
                        help="add the shadow recipient recommendation (never applied)")
    parser.add_argument("--min-samples", type=int, default=100,
                        help="labelled recipient rows required before advising")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    samples: list[LearningSample] = []
    if args.store:
        samples.extend(load_store(args.store))
    if args.annotations:
        records = load_annotations(args.annotations)
        if not args.bot_id and any("bot_targeted" in row for row in records):
            # Silence here would look like a perfect recipient model.
            print("警告：标注里有收件人标签，但未提供 --bot-id，收件人样本已跳过",
                  file=sys.stderr)
        samples.extend(samples_from_annotations(records, bot_id=args.bot_id))
    if not samples:
        print("没有可用的样本：请提供 --annotations 或 --store", file=sys.stderr)
        return 1

    if args.save:
        args.save.write_text("\n".join(json.dumps(s.to_dict(), ensure_ascii=False,
                                                   allow_nan=False) for s in samples) + "\n",
                             encoding="utf-8")

    report = summarize(samples, min_support=max(1, args.min_support))
    advice = (analyze_recipient(samples, min_samples=max(1, args.min_samples),
                                min_support=max(1, args.min_support))
              if args.recommend else None)
    if args.json:
        payload = dict(report)
        if advice is not None:
            payload["recommendation"] = {
                "task": advice.task, "ready": advice.ready, "samples": advice.samples,
                "required_samples": advice.required_samples, "notes": list(advice.notes),
                "factors": [vars(item) for item in advice.factors]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(report))
        if advice is not None:
            print()
            print(advice.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
