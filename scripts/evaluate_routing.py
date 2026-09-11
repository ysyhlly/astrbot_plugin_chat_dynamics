"""Replay deterministic, offline routing fixtures against the current root package."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_chat_dynamics.core import thread_router  # noqa: E402
from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter  # noqa: E402
from astrbot_plugin_chat_dynamics.core.bot_identity import BotIdentityMatcher  # noqa: E402
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG  # noqa: E402
from astrbot_plugin_chat_dynamics.core.message_semantics import describe_message  # noqa: E402
from astrbot_plugin_chat_dynamics.core.persona_engine import turn_is_addressed  # noqa: E402
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime  # noqa: E402
from astrbot_plugin_chat_dynamics.core.routing_trace import build_routing_trace  # noqa: E402
from astrbot_plugin_chat_dynamics.core.routing_contract import ROUTING_WEIGHTS_VERSION as WEIGHTS_VERSION  # noqa: E402
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext  # noqa: E402


CONFIGURATION = {"require_intense_dialogue": False, "clock": 1000,
                 "neural": False, "strong_threshold": 0.70, "hover_threshold": 0.40,
                 "time_decay_half_life": 45.0}


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def compare_reports(report, baseline):
    """Compare outcome values by case id without mutating either report."""
    old = {row["id"]: row for row in baseline["results"]}
    new = {row["id"]: row for row in report["results"]}
    def comparable(before, after):
        """Compare the fields both reports recorded; new fields are not regressions."""
        return {name: value for name, value in after.items() if name in before} != before

    return {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "changes": [{"id": key, "before": old[key]["actual"], "after": new[key]["actual"]}
                        for key in sorted(old.keys() & new.keys())
                        if comparable(old[key]["actual"], new[key]["actual"])],
            "failed_delta": report["failed"] - baseline["failed"],
            "fixture_hash_matches": (baseline.get("fixture_hash") == report["fixture_hash"]
                                     if baseline.get("fixture_hash") else None)}


def evaluate(cases):
    results = []
    for case in cases:
        runtime = SessionRuntime("golden", "golden", "golden", bot_id="bot", dag=ConversationDAG())
        router = thread_router.ThreadRouter(require_intense_dialogue=CONFIGURATION["require_intense_dialogue"])
        names = case.get("bot_names", ["群间"])
        for i, message in enumerate(case["messages"]):
            node = runtime.dag.add_message(str(i), message.get("user", "A"), message["text"],
                                          timestamp=float(CONFIGURATION["clock"]) + i,
                                          mentioned_users=message.get("mentions", []),
                                          reply_to_id=message.get("reply_to"))
            router.route(runtime, node, bot_names=names)
            if node.user_id == "bot":
                runtime.last_bot_node = node
        # This fixture boundary deliberately injects independent topic uncertainty.
        if case.get("force_topic_ambiguous"):
            node.metadata["routing"]["topic_ambiguous"] = True
            node.metadata["routing"]["ambiguous"] = True
        score = AddressivityRouter(
            bot_id="bot", bot_names=names, strong_threshold=CONFIGURATION["strong_threshold"],
            hover_threshold=CONFIGURATION["hover_threshold"],
            time_decay_half_life=CONFIGURATION["time_decay_half_life"]).compute_addressivity(
            node, runtime.dag, last_bot_node=runtime.last_bot_node, runtime=runtime)
        semantics = describe_message(node, runtime.dag, bot_id="bot")
        turn = TurnContext("golden", node.user_id, node.text,
                           (MessageSnapshot(node.msg_id, node.user_id, node.text),), (), 0, 0,
                           node.timestamp, "bot" in node.mentioned_users)
        actual = {"recipients": sorted(semantics.recipient_ids),
                  "bot_targeted": score.is_bot_targeted,
                  "persona_addressed": turn_is_addressed(runtime, turn, node.timestamp),
                  # Locks the strong/hover/weak boundary, including silent hover buffering.
                  "level": score.level.value}
        failures = {key: {"expected": value, "actual": actual[key]}
                    for key, value in case["expected"].items() if actual[key] != value}
        identity = asdict(BotIdentityMatcher.match(node.text, names,
                                                  mentions=node.mentioned_users, bot_id="bot"))
        identity["bot_reference"] = next((key for key in ("mention", "vocative", "subject")
                                          if identity[key]), "none")
        traces = {}
        for mode in ("legacy", "persona"):
            traces[mode] = build_routing_trace(
                routing=node.metadata["routing"], identity=identity,
                participation={"score": score.score if mode == "legacy" else None,
                               "level": score.level if mode == "legacy" else None,
                               "should_reply": None},
                state={"pending_hover": False,
                       "active_interlocutor": getattr(runtime, "last_interlocutor", ""),
                       "intervening_users": None}, mode=mode, weights_version=WEIGHTS_VERSION)
        results.append({"id": case["id"], "actual": actual, "failures": failures, "traces": traces})
    confusion = {}
    for mode, key in (("legacy", "bot_targeted"), ("persona", "persona_addressed")):
        counts = dict(tp=0, fp=0, tn=0, fn=0)
        for case, row in zip(cases, results):
            if key in case["expected"]:
                expected, actual = case["expected"][key], row["actual"][key]
                counts[("t" if expected == actual else "f") + ("p" if actual else "n")] += 1
        confusion[mode] = counts
    return {"schema_version": 1, "clock": 1000, "neural": False,
            "configuration": dict(CONFIGURATION), "config_hash": content_hash(CONFIGURATION),
            "weights_version": WEIGHTS_VERSION, "fixture_hash": content_hash(cases),
            "confusion": confusion,
            "total": len(results), "failed": sum(bool(row["failures"]) for row in results),
            "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures", type=Path, default=ROOT / "tests/fixtures/routing_golden.json")
    parser.add_argument("--baseline", type=Path, help="Read-only comparison with a prior JSON report")
    parser.add_argument("--check", action="store_true", help="Exit nonzero for golden mismatches")
    args = parser.parse_args()
    if Path(thread_router.__file__).resolve() != ROOT / "core" / "thread_router.py":
        raise RuntimeError("Evaluator did not load the current repository root package")
    if args.output and args.baseline and args.output.resolve() == args.baseline.resolve():
        parser.error("--output must not overwrite --baseline")
    if args.output and args.output.resolve() == args.fixtures.resolve():
        parser.error("--output must not overwrite --fixtures")
    report = evaluate(json.loads(args.fixtures.read_text(encoding="utf-8")))
    if args.baseline:
        report["comparison"] = compare_reports(report, json.loads(args.baseline.read_text(encoding="utf-8")))
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return int(args.check and report["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
