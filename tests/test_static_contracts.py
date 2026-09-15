"""Static contracts: the invariants no runtime test can see.

Four kinds of defect in this repository were invisible to behavioural tests, because
each one is a disagreement between two places in the *source*:

* a node-metadata key a reader trusts and no producer writes ("is_bot",
  "trigger_user_id", "routing_trace", "addressivity");
* a wall-clock bookkeeping call handed the monotonic turn clock, which rolled the
  manners day bucket back to 1970 on every withheld turn;
* a metric recorded under a name outside the registry, so the value was persisted
  and then dropped on the next restart;
* two copies of the same translation string drifting apart.

Each test reads the tree instead of importing it, so a new key or call site has to
be deliberate: the failure message says what to do about it.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Production sources only: tests may fabricate whatever they need.
_SOURCE_PATHS = [ROOT / "main.py"] + sorted((ROOT / "core").rglob("*.py"))

def _sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in _SOURCE_PATHS
    }


def _constants(tree: ast.Module) -> dict[str, str]:
    """Resolve literal module constants locally, never across unrelated modules."""
    found = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        value = getattr(node, "value", None)
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found[target.id] = value.value
    return found


def _is_metadata(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "metadata") or (
        isinstance(node, ast.Attribute) and node.attr == "metadata"
    )


def _metadata_keys(texts: dict[str, str]) -> tuple[set[str], set[str]]:
    """Top-level literal metadata keys; dynamic values are intentionally unknown."""
    written: set[str] = set()
    read: set[str] = set()
    for text in texts.values():
        tree = ast.parse(text)
        constants = _constants(tree)

        def key(expression):
            if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
                return expression.value
            if isinstance(expression, ast.Name):
                return constants.get(expression.id)
            return None

        def write_dict(expression):
            if isinstance(expression, ast.Dict):
                for dict_key, value in zip(expression.keys, expression.values):
                    literal = key(dict_key)
                    if literal is not None:
                        written.add(literal)
                    elif dict_key is None:
                        write_dict(value)  # A literal **mapping expands top-level keys.

        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and _is_metadata(node.value):
                literal = key(node.slice)
                if literal is not None:
                    if isinstance(node.ctx, ast.Store):
                        written.add(literal)
                    elif isinstance(node.ctx, ast.Load):
                        read.add(literal)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(_is_metadata(target) for target in targets):
                    write_dict(node.value)
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "metadata":
                        write_dict(keyword.value)
                function = node.func
                if not isinstance(function, ast.Attribute) or not _is_metadata(function.value):
                    continue
                if function.attr in {"get", "setdefault"} and node.args:
                    literal = key(node.args[0])
                    if literal is not None:
                        read.add(literal)
                        if function.attr == "setdefault":
                            written.add(literal)
                elif function.attr == "update":
                    for argument in node.args:
                        write_dict(argument)
                    for keyword in node.keywords:
                        if keyword.arg is not None:
                            written.add(keyword.arg)
                        else:
                            write_dict(keyword.value)
    return written, read


def test_metadata_ast_ignores_comments_comparisons_and_nested_dict_keys():
    written, read = _metadata_keys({"sample.py": """
# metadata['comment'] = True
metadata['compared'] == True
metadata = {'outer': {'inner': 1}}
node.metadata = {'assigned': True}
make_node(metadata={'keyword': {'nested': 2}})
metadata['stored'] = 1
metadata.get('looked_up')
metadata.setdefault('defaulted', {})
metadata.update({'updated': {'not_top_level': 1}}, named=True, **{'expanded': 1})
"""})
    assert written == {"outer", "assigned", "keyword", "stored", "defaulted", "updated", "named", "expanded"}
    assert read == {"compared", "looked_up", "defaulted"}


def test_metadata_constants_are_resolved_per_module():
    written, read = _metadata_keys({
        "first.py": "KEY = 'first'\nmetadata[KEY] = 1\nmetadata.get(KEY)",
        "second.py": "KEY: str = 'second'\nmetadata.setdefault(KEY, 1)",
        "third.py": "metadata[KEY] = 1\nmetadata.get(KEY)",
    })
    assert written == read == {"first", "second"}


def test_every_persisted_metadata_key_has_a_producer():
    """A key nobody writes is a phantom: a reader will trust it and read nothing."""
    from astrbot_plugin_chat_dynamics.core.runtime_persistence import _WRITTEN_META_FIELDS

    written, _read = _metadata_keys(_sources())

    missing = sorted(set(_WRITTEN_META_FIELDS) - written)
    assert not missing, (
        "persisted metadata keys with no producer: "
        f"{missing}. Write them where the node is created, or move them to "
        "_LEGACY_META_FIELDS if they only exist in older snapshots."
    )


def test_legacy_metadata_keys_stay_legacy():
    """Nothing may start writing a key that only exists to read old snapshots."""
    from astrbot_plugin_chat_dynamics.core.runtime_persistence import _LEGACY_META_FIELDS

    written, _read = _metadata_keys(_sources())

    revived = sorted(set(_LEGACY_META_FIELDS) & written)
    assert not revived, (
        f"legacy metadata keys are being written again: {revived}. "
        "Move them into _WRITTEN_META_FIELDS so the producer test covers them."
    )


def test_no_metadata_key_is_read_without_a_writer():
    """The reader side of the same rule, which is how the phantom bugs surfaced."""
    written, read = _metadata_keys(_sources())

    orphans = sorted(key for key in read - written if not key.startswith("_"))
    assert not orphans, (
        f"metadata keys are read but never written: {orphans}. "
        "Either write them or stop reading them."
    )


# --- wall-clock bookkeeping ------------------------------------------------

_BOOKKEEPING = {"note_quiet", "note_intervene", "note_spoke", "note_arbiter_silence"}
_GATE_RECEIVERS = {"decision_gate", "gate_engine"}
_WALL_LOCALS = {"main.py": {"wall_now"}, "core/persona_engine.py": {"gate_now"}}


def _wall_call(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr == "wall_time"
        and not expression.args and not expression.keywords
    )


def _clock_offenders(name: str, source: str) -> tuple[int, list[str]]:
    checked = 0
    offenders = []
    for call in ast.walk(ast.parse(source)):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        receiver = call.func.value
        receiver_name = receiver.attr if isinstance(receiver, ast.Attribute) else (
            receiver.id if isinstance(receiver, ast.Name) else ""
        )
        if receiver_name not in _GATE_RECEIVERS:
            continue
        method = call.func.attr
        if method not in _BOOKKEEPING | {"evaluate"}:
            continue
        checked += 1
        for keyword in call.keywords:
            if keyword.arg not in {"now", "current_time"}:
                continue
            value = keyword.value
            allowed = method == "evaluate" and (
                _wall_call(value) or
                isinstance(value, ast.Name) and value.id in _WALL_LOCALS.get(name, set())
            )
            if not allowed:
                offenders.append(f"{name}:{call.lineno} {method}({keyword.arg}=...)")
    return checked, offenders


def test_wall_clock_bookkeeping_is_never_handed_the_turn_clock():
    checked = 0
    offenders = []
    for name, source in _sources().items():
        count, errors = _clock_offenders(name, source)
        checked += count
        offenders.extend(errors)
    assert checked, "No gate calls found; update the AST receiver inventory"
    assert not offenders, "Gate bookkeeping owns its wall clock:\n" + "\n".join(offenders)


def test_clock_guard_detects_nested_calls_and_ignores_node_clock():
    source = """
gate_engine.note_spoke('s', skin=make_skin(')'), now=clock.wall_time())
self.decision_gate.note_quiet('s', now=turn_clock)
gate_engine.evaluate(now=clock.wall_time(), node_now=clock.time())
"""
    checked, offenders = _clock_offenders("example.py", source)
    assert checked == 3
    assert len(offenders) == 2


def test_gate_bookkeeping_signatures_do_not_accept_timestamps():
    tree = ast.parse((ROOT / "core/decision_gate.py").read_text(encoding="utf-8"))
    gate = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DynamicsDecisionGate")
    methods = {node.name: node for node in gate.body if isinstance(node, ast.FunctionDef)}
    assert _BOOKKEEPING <= methods.keys()
    for name in _BOOKKEEPING:
        arguments = methods[name].args
        assert not {"now", "current_time"} & {arg.arg for arg in arguments.args + arguments.kwonlyargs}
        assert arguments.kwarg is None


# --- metrics registry ------------------------------------------------------

def _metric_names(expression: ast.expr) -> list[str]:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return [expression.value]
    if isinstance(expression, ast.IfExp):
        return _metric_names(expression.body) + _metric_names(expression.orelse)
    return []


def test_every_recorded_metric_name_is_in_the_registry():
    """A name outside the registry is persisted and then dropped on the next restore."""
    from astrbot_plugin_chat_dynamics.main import _METRIC_NAMES

    known = set(_METRIC_NAMES)
    offenders: list[str] = []
    checked = 0
    for name, text in _sources().items():
        for call in ast.walk(ast.parse(text)):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            function = call.func
            if not ((isinstance(function, ast.Attribute) and function.attr == "_metric")
                    or (isinstance(function, ast.Name) and function.id == "_metric")):
                continue
            checked += 1
            for metric in _metric_names(call.args[0]):
                if metric not in known:
                    offenders.append(f"{name}:{call.lineno} _metric({metric!r})")
    assert checked, "No metric calls found; update the AST metric inventory"
    assert not offenders, (
        "metrics recorded under names missing from _METRIC_NAMES "
        "(they will not survive a restart):\n" + "\n".join(offenders)
    )


# --- duplicated translations -----------------------------------------------

def _flatten(payload: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in payload.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}."))
        elif isinstance(value, str):
            flat[name] = value
    return flat


def test_both_translation_trees_agree_on_every_shared_key():
    """The plugin ships two i18n files; a shared key with two values is a bug."""
    for locale in ("zh-CN", "en-US"):
        flat = _flatten(json.loads((ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8")))
        manifest = _flatten(json.loads(
            (ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(encoding="utf-8")))
        # The manifest nests the plugin name under metadata; the flat file calls it
        # plugin_name. Compare it only when the manifest actually carries one.
        display = manifest.get("metadata.display_name")
        if display:
            manifest["plugin_name"] = display
        disagreements = {
            key: (flat[key], manifest[key])
            for key in set(flat) & set(manifest)
            if flat[key] != manifest[key]
        }
        assert not disagreements, f"{locale}: translation copies disagree: {disagreements}"
