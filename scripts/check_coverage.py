"""Enforce the v1.2.0 production coverage contract.

The pytest coverage plugin has one global threshold.  This small checker keeps
the more useful per-module gates in CI as well, so a large, well-covered module
cannot hide a regression in the web, send, session, or lifecycle paths.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_MODULE_THRESHOLDS = {
    "core/web_api.py": 90.0,
    "core/web_compat.py": 90.0,
    "core/platform_bridge.py": 90.0,
    "core/debounce.py": 90.0,
    "core/session_runtime.py": 90.0,
}


def _normalise(path: str) -> str:
    return path.replace("\\", "/").lower()


def _find_file(files: dict[str, Any], suffix: str) -> tuple[str, dict[str, Any]] | None:
    wanted = _normalise(suffix)
    matches = [(name, value) for name, value in files.items() if _normalise(name).endswith(wanted)]
    if not matches:
        return None
    return sorted(matches, key=lambda item: item[0])[0]


def _percent(summary: dict[str, Any]) -> float:
    return float(summary.get("percent_covered", 0.0) or 0.0)


def validate_coverage(
    report: dict[str, Any],
    *,
    total_threshold: float = 90.0,
    main_threshold: float = 85.0,
    module_thresholds: dict[str, float] | None = None,
) -> list[str]:
    errors: list[str] = []
    totals = report.get("totals")
    files = report.get("files")
    if not isinstance(totals, dict) or not isinstance(files, dict):
        return ["coverage JSON must contain object keys 'totals' and 'files'"]

    total = _percent(totals)
    if total < total_threshold:
        errors.append(f"total coverage {total:.2f}% is below {total_threshold:.2f}%")

    main_match = _find_file(files, "main.py")
    if main_match is None:
        errors.append("coverage JSON is missing main.py")
    else:
        main = _percent(main_match[1].get("summary", {}))
        if main < main_threshold:
            errors.append(f"main.py coverage {main:.2f}% is below {main_threshold:.2f}%")

    for suffix, threshold in (module_thresholds or DEFAULT_MODULE_THRESHOLDS).items():
        match = _find_file(files, suffix)
        if match is None:
            errors.append(f"coverage JSON is missing {suffix}")
            continue
        covered = _percent(match[1].get("summary", {}))
        if covered < threshold:
            errors.append(f"{suffix} coverage {covered:.2f}% is below {threshold:.2f}%")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=Path("artifacts/coverage.json"))
    parser.add_argument("--total", type=float, default=90.0)
    parser.add_argument("--main", dest="main_threshold", type=float, default=85.0)
    args = parser.parse_args(argv)

    try:
        report = json.loads(args.json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Coverage check failed: cannot read {args.json}: {type(exc).__name__}", file=sys.stderr)
        return 1
    errors = validate_coverage(
        report,
        total_threshold=args.total,
        main_threshold=args.main_threshold,
    )
    if errors:
        print("Coverage check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        f"Coverage check passed: total={_percent(report['totals']):.2f}% "
        f"main={_percent(_find_file(report['files'], 'main.py')[1]['summary']):.2f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
