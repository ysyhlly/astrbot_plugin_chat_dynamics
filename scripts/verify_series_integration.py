"""Cross-8-plugin integration smoke check & release gate for the Chat Dynamics series."""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SERIES_PLUGINS = (
    "astrbot_plugin_chat_dynamics",
    "astrbot_plugin_dynamics_world",
    "astrbot_plugin_dynamics_learning",
    "astrbot_plugin_dynamics_memory",
    "astrbot_plugin_dynamics_horizons",
    "astrbot_plugin_dynamics_games",
    "astrbot_plugin_visual_media",
    "astrbot_plugin_group_chronicle",
)
_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "vendor",
        "tests",
        "test",
        "integration",
        "integration_tests",
        "scripts",
        "tools",
    }
)


def check_no_stdlib_logging(workspace: Path) -> list[str]:
    """Ensure no production plugin Python module imports stdlib logging."""
    errors: list[str] = []
    for name in SERIES_PLUGINS:
        repo = workspace / name
        if not repo.is_dir():
            continue
        for py in sorted(repo.rglob("*.py")):
            if set(py.relative_to(repo).parts) & _SKIP_DIRS:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8-sig"), filename=str(py))
            except (OSError, SyntaxError) as exc:
                errors.append(f"parse error in {name}/{py.relative_to(repo).as_posix()}: {exc}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(a.name == "logging" or a.name.startswith("logging.") for a in node.names):
                        errors.append(f"stdlib logging in {name}/{py.relative_to(repo).as_posix()}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if mod == "logging" or mod.startswith("logging."):
                        errors.append(f"stdlib logging in {name}/{py.relative_to(repo).as_posix()}:{node.lineno}")
    return errors


def check_health_hooks_present(workspace: Path) -> list[str]:
    """Verify every installed series plugin defines get_series_health in main.py."""
    errors: list[str] = []
    for name in SERIES_PLUGINS:
        main_py = workspace / name / "main.py"
        if not main_py.is_file():
            continue
        src = main_py.read_text(encoding="utf-8-sig")
        if "def get_series_health(" not in src:
            errors.append(f"{name}/main.py missing get_series_health()")
    return errors


def _install_minimal_astrbot_stubs() -> None:
    """Install lightweight AstrBot module stubs only when running offline without AstrBot."""
    if "astrbot" in sys.modules:
        return
    try:
        if importlib.util.find_spec("astrbot") is not None:
            return
    except (ImportError, ValueError):
        pass

    class _SilentLogger:
        def debug(self, *_args, **_kwargs):
            return None

        def info(self, *_args, **_kwargs):
            return None

        def warning(self, *_args, **_kwargs):
            return None

        def error(self, *_args, **_kwargs):
            return None

        def exception(self, *_args, **_kwargs):
            return None

    def _pkg(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    astrbot_pkg = _pkg("astrbot")
    api_pkg = _pkg("astrbot.api")
    web_mod = types.ModuleType("astrbot.api.web")
    star_mod = types.ModuleType("astrbot.api.star")
    event_mod = types.ModuleType("astrbot.api.event")
    mc_mod = types.ModuleType("astrbot.api.message_components")
    core_pkg = _pkg("astrbot.core")
    prov_pkg = _pkg("astrbot.core.provider")
    entities_mod = types.ModuleType("astrbot.core.provider.entities")

    api_pkg.logger = _SilentLogger()  # type: ignore[attr-defined]
    web_mod.request = None  # type: ignore[attr-defined]
    web_mod.json_response = lambda data, status_code=200: (status_code, data)  # type: ignore[attr-defined]
    web_mod.error_response = lambda msg, status_code=400, **kw: (status_code, {"status": "error", "message": msg, **kw})  # type: ignore[attr-defined]
    star_mod.Context = object  # type: ignore[attr-defined]
    star_mod.Star = object  # type: ignore[attr-defined]
    event_mod.AstrMessageEvent = object  # type: ignore[attr-defined]
    for cname in ("Plain", "At", "Reply", "Image", "Face", "Poke", "Record", "File", "Video"):
        setattr(mc_mod, cname, type(cname, (), {}))
    entities_mod.LLMResponse = SimpleNamespace  # type: ignore[attr-defined]
    entities_mod.TokenUsage = SimpleNamespace  # type: ignore[attr-defined]

    astrbot_pkg.api = api_pkg  # type: ignore[attr-defined]
    astrbot_pkg.core = core_pkg  # type: ignore[attr-defined]
    api_pkg.web = web_mod  # type: ignore[attr-defined]
    api_pkg.star = star_mod  # type: ignore[attr-defined]
    api_pkg.event = event_mod  # type: ignore[attr-defined]
    api_pkg.message_components = mc_mod  # type: ignore[attr-defined]
    core_pkg.provider = prov_pkg  # type: ignore[attr-defined]
    prov_pkg.entities = entities_mod  # type: ignore[attr-defined]

    sys.modules["astrbot.api.web"] = web_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.core.provider.entities"] = entities_mod


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def check_cross_plugin_runtime_contracts(workspace: Path) -> list[str]:
    """Verify cross-plugin silent provider lookup, presence_epoch invalidation, and telemetry."""
    errors: list[str] = []
    _install_minimal_astrbot_stubs()
    chat_root = workspace / "astrbot_plugin_chat_dynamics"
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))

    cd_panels = _load_module("verify_cd_panels", chat_root / "series_panels.py")
    world_panels_path = workspace / "astrbot_plugin_dynamics_world" / "series_panels.py"
    world_panels = (
        _load_module("verify_dw_panels", world_panels_path)
        if world_panels_path.is_file()
        else cd_panels
    )

    # 1) Cold-start silent provider lookup must never call get_provider_by_id when inst_map exists
    noisy_calls: list[str] = []
    ctx = SimpleNamespace(
        provider_manager=SimpleNamespace(inst_map={}),
        get_provider_by_id=lambda pid: (noisy_calls.append(pid), None)[1],
    )
    if cd_panels.find_provider_silently(ctx, "qw") is not None:
        errors.append("cold-start silent lookup unexpectedly found absent provider")
    if noisy_calls:
        errors.append(f"cold-start silent lookup triggered noisy get_provider_by_id: {noisy_calls}")

    # Simulate provider load phase
    fake_prov = SimpleNamespace(id="qw")
    ctx.provider_manager.inst_map["qw"] = fake_prov
    if world_panels.find_provider_silently(ctx, "qw") is not fake_prov:
        errors.append("post-load silent lookup failed to resolve provider from inst_map")

    # 2) Cross-plugin presence_epoch invalidation across separate module instances
    e0 = cd_panels.get_presence_epoch(ctx)
    world_panels.invalidate_presence_cache("astrbot_plugin_dynamics_world", context=ctx)
    e1 = cd_panels.get_presence_epoch(ctx)
    if e1 <= e0:
        errors.append(f"cross-plugin invalidate_presence_cache did not bump shared epoch ({e0} -> {e1})")

    # 3) End-to-end ProviderBudget + SeriesTelemetryStore across companion series_llm_call
    from astrbot_plugin_chat_dynamics.core.provider_budget import attach_context_telemetry
    from astrbot_plugin_chat_dynamics.core.series_telemetry import SeriesTelemetryStore

    with tempfile.TemporaryDirectory() as td:
        store = SeriesTelemetryStore(Path(td) / "tel.sqlite3", flush_threshold=1)
        try:
            attach_context_telemetry(ctx, store)
            await world_panels.series_llm_call(
                ctx,
                "qw",
                "astrbot_plugin_dynamics_world",
                "life",
                lambda: asyncio.sleep(0, result={"usage": {"prompt_tokens": 500, "completion_tokens": 100}}),
                model="qwen-plus",
                group_id="qq:GroupMessage:12345",
            )
            world_panels.record_series_llm_usage(
                ctx,
                plugin_id="astrbot_plugin_visual_media",
                provider_id="dashscope#1",
                purpose="image_gen",
                model="wanx",
                image_count=1,
                ok=True,
            )
            store.flush()
            snap = store.snapshot("24h")
            if snap["summary"]["calls"] != 2:
                errors.append(f"expected 2 telemetry calls, got {snap['summary']['calls']}")
            if snap["summary"]["image_count"] != 1:
                errors.append(f"expected 1 image_count, got {snap['summary']['image_count']}")
            if snap["summary"]["estimated_cost_usd"] <= 0:
                errors.append("expected positive estimated_cost_usd")
        finally:
            store.close()

    return errors


def check_boot_log_health(boot_log_path: Path) -> list[str]:
    """Validate an AstrBot boot log for provider-not-found spam and plugin errors."""
    errors: list[str] = []
    if not boot_log_path.is_file():
        return [f"boot log not found: {boot_log_path}"]
    text = boot_log_path.read_text(encoding="utf-8", errors="ignore")
    missing_prov = [
        line for line in text.splitlines() if "was not found. Its provider or model ID may have been changed." in line
    ]
    if missing_prov:
        errors.append(f"boot log contains {len(missing_prov)} 'Provider was not found' warnings")
    fatal_lines = [
        line for line in text.splitlines()
        if "[ERRO]" in line and any(pid in line for pid in SERIES_PLUGINS)
    ]
    if fatal_lines:
        errors.append(f"boot log contains {len(fatal_lines)} series plugin ERROR lines")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--skip-asset-sync", action="store_true")
    parser.add_argument("--boot-log", type=Path, default=None)
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()

    all_errors: list[str] = []
    if not args.skip_asset_sync:
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        try:
            from .sync_series_assets import check_workspace
        except ImportError:
            from sync_series_assets import check_workspace

        all_errors.extend(check_workspace(workspace))

    all_errors.extend(check_no_stdlib_logging(workspace))
    all_errors.extend(check_health_hooks_present(workspace))
    all_errors.extend(asyncio.run(check_cross_plugin_runtime_contracts(workspace)))
    if args.boot_log is not None:
        all_errors.extend(check_boot_log_health(args.boot_log))

    if all_errors:
        print(f"Series integration check FAILED ({len(all_errors)} issues):", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"Series integration check PASSED across {len(SERIES_PLUGINS)} plugins.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
