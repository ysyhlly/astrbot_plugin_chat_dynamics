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
from astrbot_plugin_chat_dynamics.core.routing_metrics import routing_metrics  # noqa: E402
from astrbot_plugin_chat_dynamics.core.routing_metrics import ratio  # noqa: E402
from astrbot_plugin_chat_dynamics.core.routing_contract import ROUTING_WEIGHTS_VERSION as WEIGHTS_VERSION  # noqa: E402
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext  # noqa: E402


CONFIGURATION = {"require_intense_dialogue": False, "clock": 1000,
                 "neural": False, "strong_threshold": 0.70, "hover_threshold": 0.40,
                 "time_decay_half_life": 45.0}


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def metric_constraints(metrics):
    """Only supervised, observable metrics impose regression constraints."""
    topic, parent = metrics["topic"], metrics["parent"]
    checked = {}
    if topic["pairs"]:
        checked["topic_wrong_merge"] = topic["wrong_merge"] == 0
        checked["topic_fragmentation"] = topic["fragmentation"] == 0
    if parent["labeled"]:
        checked["parent_exact"] = parent["correct"] == parent["labeled"]
    return checked


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


def turn_state(runtime, node):
    """Snapshot the cross-message state a scored turn actually saw."""
    dialogue = runtime.active_dialogue
    last_bot = runtime.last_bot_node
    recent = runtime.dag.get_recent_nodes(limit=80)
    return {"pending_hover": bool(runtime.pending_hover),
            "pending_hover_user": str(getattr(runtime.pending_hover, "user_id", "") or "") or None,
            "active_interlocutor": (dialogue.user_id if dialogue else runtime.last_interlocutor) or None,
            "last_bot_message_id": dialogue.last_bot_message_id if dialogue else None,
            "last_bot_was_question": dialogue.last_bot_was_question if dialogue else None,
            "waiting_for_answer": bool(dialogue and dialogue.accepts_answer(node, recent)),
            "intervening_users": len({n.user_id for n in recent
                if last_bot is not None and last_bot.timestamp < n.timestamp < node.timestamp
                and n.user_id not in {runtime.bot_id, node.user_id}})}


def scored_actual(runtime, node, score, semantics):
    turn = TurnContext("golden", node.user_id, node.text,
                       (MessageSnapshot(node.msg_id, node.user_id, node.text),), (), 0, 0,
                       node.timestamp, "bot" in node.mentioned_users)
    return {"recipients": sorted(semantics.recipient_ids),
            "bot_targeted": score.is_bot_targeted,
            "persona_addressed": turn_is_addressed(runtime, turn, node.timestamp),
            # Locks the strong/hover/weak boundary, including silent hover buffering.
            "level": score.level.value}


def mismatches(expected, actual):
    return {key: {"expected": value, "actual": actual[key]}
            for key, value in expected.items() if actual[key] != value}


def evaluate(cases):
    results = []
    sessions = []
    for case in cases:
        runtime = SessionRuntime("golden", "golden", "golden", bot_id="bot", dag=ConversationDAG())
        router = thread_router.ThreadRouter(require_intense_dialogue=CONFIGURATION["require_intense_dialogue"])
        addressivity_router = AddressivityRouter(
            bot_id="bot", strong_threshold=CONFIGURATION["strong_threshold"],
            hover_threshold=CONFIGURATION["hover_threshold"],
            time_decay_half_life=CONFIGURATION["time_decay_half_life"])
        names = case.get("bot_names", ["群间"])
        observations = []
        turns = []
        scored = None
        last_index = len(case["messages"]) - 1
        for i, message in enumerate(case["messages"]):
            # "at" allows a deterministic gap so hover TTL and stale-conversation
            # boundaries can be replayed; the default keeps one-second steps.
            node = runtime.dag.add_message(str(i), message.get("user", "A"), message["text"],
                                          timestamp=float(CONFIGURATION["clock"]) + message.get("at", i),
                                          mentioned_users=message.get("mentions", []),
                                          reply_to_id=message.get("reply_to"))
            router.route(runtime, node, bot_names=names)
            routing = node.metadata["routing"]
            observation = {"message_id": node.msg_id, "topic_id": routing.get("topic_id"),
                           "parent_message_id": routing.get("parent_message_id")}
            if "topic_candidates" in routing:
                observation["topic_candidates"] = routing["topic_candidates"]
            for key in ("expected_topic", "expected_parent"):
                if key in message:
                    observation[key] = message[key]
            observations.append(observation)
            if node.user_id == "bot":
                # Production records bot output as an anchor, not as a scored turn.
                runtime.last_bot_node = node
                continue
            # This fixture boundary deliberately injects independent topic uncertainty.
            if case.get("force_topic_ambiguous") and i == last_index:
                routing["topic_ambiguous"] = True
                routing["ambiguous"] = True
            # Replay runs the production order: expire, score against committed
            # state, then commit this turn's own decision.
            runtime.expire_hovers(node.timestamp)
            score = addressivity_router.compute_addressivity(
                node, runtime.dag, last_bot_node=runtime.last_bot_node, runtime=runtime,
                prior_hover=runtime.pending_hover, prior_hovers=list(runtime.pending_hovers))
            semantics = describe_message(node, runtime.dag, bot_id="bot")
            actual = scored_actual(runtime, node, score, semantics)
            turns.append({"index": i, "message_id": node.msg_id, "actual": actual,
                          "failures": mismatches(message.get("expected", {}), actual),
                          "state": turn_state(runtime, node)})
            runtime.commit_participation(node, score.level, node.timestamp)
            scored = (node, score, semantics, actual)
        if scored is None:
            raise ValueError(f"case {case.get('id')!r} has no scored message")
        node, score, semantics, actual = scored
        failures = mismatches(case.get("expected", {}), actual)
        for turn in turns:
            failures.update({f"turn{turn['index']}.{key}": detail
                             for key, detail in turn["failures"].items()})
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
                               "should_reply": None,
                               "evidence": score.evidence,
                               "family_contributions": score.family_contributions,
                               "contribution_total": score.contribution_total},
                state={**turn_state(runtime, node), "pending_hover": False},
                mode=mode, weights_version=WEIGHTS_VERSION)
        sessions.append(observations)
        results.append({"id": case["id"], "actual": actual, "failures": failures,
                        "traces": traces, "turns": turns, "observations": observations})
    confusion = {}
    for mode, key in (("legacy", "bot_targeted"), ("persona", "persona_addressed")):
        counts = dict(tp=0, fp=0, tn=0, fn=0)
        for case, row in zip(cases, results):
            if key in case.get("expected", {}):
                expected, actual = case["expected"][key], row["actual"][key]
                counts[("t" if expected == actual else "f") + ("p" if actual else "n")] += 1
        confusion[mode] = counts
    groups = {}
    for case, row in zip(cases, results):
        group = case.get("group", "ungrouped")
        counts = groups.setdefault(group, {mode: dict(tp=0, fp=0, tn=0, fn=0)
                                           for mode in ("legacy", "persona")})
        for mode, key in (("legacy", "bot_targeted"), ("persona", "persona_addressed")):
            expected = case.get("expected", {}).get(key)
            if isinstance(expected, bool):
                actual = row["actual"][key]
                counts[mode][("t" if expected == actual else "f") + ("p" if actual else "n")] += 1
    for modes in groups.values():
        for counts in modes.values():
            counts["precision"] = ratio(counts["tp"], counts["tp"] + counts["fp"])
            counts["recall"] = ratio(counts["tp"], counts["tp"] + counts["fn"])
    metrics = routing_metrics(sessions)
    constraints = metric_constraints(metrics)
    return {"schema_version": 1, "clock": 1000, "neural": False,
            "configuration": dict(CONFIGURATION), "config_hash": content_hash(CONFIGURATION),
            "weights_version": WEIGHTS_VERSION, "fixture_hash": content_hash(cases),
            "confusion": confusion,
            "recipient_groups": groups, "routing_metrics": metrics,
            "checked_constraints": constraints,
            "metric_failures": [key for key, passed in constraints.items() if not passed],
            "total": len(results), "failed": sum(bool(row["failures"]) for row in results),
            "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures", type=Path, default=ROOT / "tests/fixtures/routing_golden.json")
    parser.add_argument("--baseline", type=Path, help="Read-only comparison with a prior JSON report")
    parser.add_argument("--check", action="store_true", help="Exit nonzero for golden mismatches")
    parser.add_argument("--benchmark", action="store_true", help="Include nondeterministic elapsed evaluation time")
    args = parser.parse_args()
    if Path(thread_router.__file__).resolve() != ROOT / "core" / "thread_router.py":
        raise RuntimeError("Evaluator did not load the current repository root package")
    if args.output and args.baseline and args.output.resolve() == args.baseline.resolve():
        parser.error("--output must not overwrite --baseline")
    if args.output and args.output.resolve() == args.fixtures.resolve():
        parser.error("--output must not overwrite --fixtures")
    cases = json.loads(args.fixtures.read_text(encoding="utf-8"))
    if args.benchmark:
        from time import perf_counter
        started = perf_counter()
    report = evaluate(cases)
    if args.benchmark:
        report["benchmark"] = {"elapsed_seconds": perf_counter() - started}
    if args.baseline:
        report["comparison"] = compare_reports(report, json.loads(args.baseline.read_text(encoding="utf-8")))
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return int(args.check and (report["failed"] > 0 or bool(report["metric_failures"])))


if __name__ == "__main__":
    raise SystemExit(main())
