"""Turn exported replay annotations into learning samples and a factor report.

Read-only with respect to the plugin: this script never touches live routing,
configuration, or AstrBot state. It reads annotation JSON (as exported from the
replay page) and prints what the labelled errors look like.

    python scripts/learning_report.py --annotations annotations.json --bot-id <id>
    python scripts/learning_report.py --annotations annotations.json --recommend
    python scripts/learning_report.py --annotations annotations.json --save samples.jsonl
    python scripts/learning_report.py --store samples.jsonl --json

Rows that could not be built are reported on stderr rather than dropped in
silence: a corpus that is quietly smaller than it looks is worse than one that
says why.
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
    annotation_metrics,
    build_samples,
    summarize,
)

MAX_RENDERED_FACTORS = 8


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


def load_store(path: Path) -> list:
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


def render_factors(rows: list) -> list:
    lines = []
    for row in rows[:MAX_RENDERED_FACTORS]:
        detail = (f"n={row['support']} 错{row['wrong_n']}/对{row['right_n']} "
                  f"覆盖{row['coverage'] * 100:.0f}%")
        buckets = row.get("buckets") or {}
        if "fp" in buckets or "fn" in buckets:
            # False positives and false negatives call for opposite corrections,
            # so they are printed apart instead of as one averaged difference.
            detail += (f" | FP均值 {buckets.get('fp', {}).get('mean', 0.0):.2f}"
                       f" FN均值 {buckets.get('fn', {}).get('mean', 0.0):.2f}"
                       f" TP均值 {buckets.get('tp', {}).get('mean', 0.0):.2f}"
                       f" TN均值 {buckets.get('tn', {}).get('mean', 0.0):.2f}")
        lines.append(f"    {row['code']:34s} {row['delta']:+.3f}  ({detail})")
    return lines


def render_candidates(metrics: dict) -> list:
    """Show which stage failed, not only how often the topic was wrong."""
    outcomes = metrics["outcomes"]
    lines = ["", "[topic 候选归因] 正确话题是否被提出，以及被提出后是否被选中"]
    for rank, counts in sorted(metrics["recall"].items(), key=lambda item: int(item[0])):
        value = counts["value"]
        shown = "无法计算" if value is None else f"{value * 100:.1f}%"
        lines.append(f"    Candidate Recall@{rank}  {shown}  ({counts['hits']}/{counts['eligible']})")
    selection = metrics["selection"]
    value = selection["value"]
    shown = "无法计算" if value is None else f"{value * 100:.1f}%"
    lines.append(f"    Selection Accuracy  {shown}  ({selection['correct']}/{selection['eligible']}"
                 "，仅在正确话题进入候选时计算)")
    lines.append("    归因  " + "  ".join(
        f"{key}:{outcomes[key]}" for key in ("selected", "ranking_error", "candidate_miss",
                                             "not_recorded", "new_topic_expected",
                                             "unattributable")))
    if metrics["candidate_lengths"]:
        lengths = "  ".join(f"{k}个:{v}" for k, v in metrics["candidate_lengths"].items())
        lines.append(f"    候选长度分布  {lengths}")
    for note in metrics["notes"]:
        lines.append(f"    注：{note}")
    return lines


def render(report: dict) -> str:
    lines = [f"样本总数 {report['total']}（{report['sample_note']}）"]
    grouping = report.get("grouping") or {}
    lines.append(f"会话分组：{grouping.get('groups', 0)} 个"
                 + (f"（{grouping['note']}）" if grouping.get("note") else ""))
    for task, data in report["tasks"].items():
        lines.append("")
        lines.append(f"[{task}] 准确 {data['accuracy'] * 100:.1f}%  "
                     f"({data['correct']}/{data['total']})")
        if data.get("outcomes"):
            outcome = data["outcomes"]
            lines.append(f"    混淆  TP={outcome['tp']} FP={outcome['fp']} "
                         f"TN={outcome['tn']} FN={outcome['fn']}")
        if data["error_counts"]:
            errors = "  ".join(f"{k}:{v}" for k, v in data["error_counts"].items())
            lines.append(f"    错误分布  {errors}")
        lines.append(f"    判对时平均置信 {data['confidence_when_right']:.2f}  "
                     f"判错时 {data['confidence_when_wrong']:.2f}")
        if len(data.get("policy_versions") or {}) > 1:
            versions = "  ".join(f"{k}:{v}" for k, v in data["policy_versions"].items())
            lines.append(f"    策略版本混杂，跨版本比较需谨慎  {versions}")
        if data["factors"]:
            lines.append("    因子差异（判错均值 - 判对均值）")
            lines += render_factors(data["factors"])
        else:
            lines.append("    因子差异：样本不足，至少需要同一因子在判错与判对两侧各出现若干次")
    return chr(10).join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--annotations", type=Path, help="annotation JSON exported from replay")
    parser.add_argument("--store", type=Path, help="existing sample JSONL to include")
    parser.add_argument("--save", type=Path, help="write the derived samples here")
    parser.add_argument("--bot-id", default="",
                        help="bot account id; required for recipient samples")
    parser.add_argument("--plugin-version", default="",
                        help="stamped into every sample so a corpus stays interpretable")
    parser.add_argument("--min-support", type=int, default=4)
    parser.add_argument("--recommend", action="store_true",
                        help="add the shadow recipient recommendation (never applied)")
    parser.add_argument("--min-samples", type=int, default=100,
                        help="labelled recipient rows required before advising")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    samples: list = []
    warnings: list = []
    records: list = []
    if args.store:
        samples.extend(load_store(args.store))
    if args.annotations:
        records = load_annotations(args.annotations)
        built = build_samples(records, bot_id=args.bot_id,
                              plugin_version=args.plugin_version)
        samples.extend(built.samples)
        warnings.extend(built.warnings())
    for warning in warnings:
        print(f"警告：{warning}", file=sys.stderr)
    if not samples:
        print("没有可用的样本：请提供 --annotations 或 --store", file=sys.stderr)
        return 1

    if args.save:
        args.save.write_text(chr(10).join(
            json.dumps(s.to_dict(), ensure_ascii=False, allow_nan=False) for s in samples) + chr(10),
            encoding="utf-8")

    report = summarize(samples, min_support=max(1, args.min_support))
    candidates = annotation_metrics(records) if records else None
    advice = (analyze_recipient(samples, min_samples=max(1, args.min_samples),
                                min_support=max(1, args.min_support))
              if args.recommend else None)
    if args.json:
        payload = dict(report)
        if candidates is not None:
            payload["candidates"] = candidates
        if advice is not None:
            payload["recommendation"] = {
                "task": advice.task, "ready": advice.ready, "samples": advice.samples,
                "required_samples": advice.required_samples,
                "reasons": list(advice.reasons), "notes": list(advice.notes),
                "findings": [item.as_dict() for item in advice.findings],
                "shadow": advice.shadow.as_dict() if advice.shadow else None}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(report))
        if candidates is not None:
            print(chr(10).join(render_candidates(candidates)))
        if advice is not None:
            print()
            print(advice.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
