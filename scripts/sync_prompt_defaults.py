"""Generate/check schema prompt defaults using only the standard library."""

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType


def prompt_schema_values(root: Path) -> dict:
    # Load the two pure instruction modules without booting AstrBot/core.__init__.
    package = "_chat_dynamics_prompt_source"
    parent = ModuleType(package)
    parent.__path__ = [str(root / "core")]
    previous = {name: module for name, module in sys.modules.items()
                if name == package or name.startswith(package + ".")}
    sys.modules[package] = parent
    try:
        for name in ("reply_length", "prompt_policy"):
            spec = importlib.util.spec_from_file_location(f"{package}.{name}", root / "core" / f"{name}.py")
            if spec is None or spec.loader is None:
                raise ValueError("prompt source missing")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        return {"decision_prompt": {"default": module.DEFAULT_DECISION_PROMPT},
                "reply_prompt": {"default": module.DEFAULT_REPLY_PROMPT, "hint": module.REPLY_PROMPT_HINT}}
    finally:
        for name in list(sys.modules):
            if name == package or name.startswith(package + "."):
                sys.modules.pop(name)
        sys.modules.update(previous)


def check_prompt_defaults(root: Path, schema: dict) -> list[str]:
    return [f"{key}.{field} differs from prompt policy; run python scripts/sync_prompt_defaults.py"
            for key, fields in prompt_schema_values(root).items() for field, value in fields.items()
            if schema.get(key, {}).get(field) != value]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = root / "_conf_schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    if args.check:
        errors = check_prompt_defaults(root, schema)
        print("\n".join(errors) if errors else "Prompt defaults are synchronized")
        return bool(errors)
    for key, fields in prompt_schema_values(root).items():
        schema[key].update(fields)
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
