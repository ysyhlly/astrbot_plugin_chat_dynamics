"""Run maintained checks for the native Jev runtime and its current pages.

Older suites document retired training/backends or the old model-decision path;
they are retained as history and are not the v1.15 release acceptance contract.
"""
from pathlib import Path
import argparse
import subprocess
import sys

BACKEND = [
    "test_builtin_systemone", "test_wake_policy", "test_strong_wake_delivery",
    "test_jev_decision", "test_jev_decision_layer", "test_jev_turn_completion", "test_participation_prompts", "test_member_stop",
    "test_turn_evidence_contract", "test_persona_explicit_context", "test_persona_reply_identity", "test_context_retrieval",
    "test_context_audit_regressions", "test_call_reply_chain", "test_paragraph_sending",
    "test_dialogue_context", "test_active_dialogue", "test_dialogue_continuity",
    "test_platform_bridge", "test_member_identity", "test_systemone_settings",
    "test_simplified_decision", "test_reply_probability_threshold", "test_config_concurrency", "test_config_fallback",
    "test_ai_review_removed", "test_topic_annotations", "test_snapshot_producers",
    "test_topic_display", "test_topic_reranker", "test_topic_formation", "test_topic_batch", "test_topic_jev",
    "test_router_enrichment_concurrency", "test_replay_decision_trace",
    "test_panel_persistence_lifecycle", "test_native_delivery_guard", "test_runtime_persistence",
    "test_graph", "test_debounce", "test_bot_identity", "test_media_gate",
    "test_decision_gate_clock", "test_release_contract",
]
BROWSER = ["test_systemone_settings_browser", "test_simplified_pages_browser", "test_ui_theme_browser", "test_replay_gantt_browser"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["backend", "browser"])
    args, pytest_args = parser.parse_known_args()
    root = Path(__file__).resolve().parents[1]
    files = ["tests/" + name + ".py" for name in (BACKEND if args.suite == "backend" else BROWSER)]
    assert all((root / name).is_file() for name in files)
    return subprocess.run([sys.executable, "-m", "pytest", *files, "-q", *pytest_args], cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
