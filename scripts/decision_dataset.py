"""Export, purge, delete and split local decision datasets without plugin imports."""

import argparse
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "decision_dataset_core", Path(__file__).resolve().parents[1] / "core" / "decision_dataset.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("purge").add_argument("--days", type=int, default=30)
    sub.add_parser("delete-session").add_argument("session")
    sub.add_parser("export").add_argument("output")
    sub.add_parser("split").add_argument("output")
    sub.add_parser("review", help="Import independent human labels from JSONL id,human_label").add_argument("input")
    sub.add_parser("audit", help="Flag recent complete requests without changing labels").add_argument("--limit", type=int, default=200)
    sub.add_parser("audit-snapshots", help="Check input integrity without reconstructing lost evidence").add_argument("--quarantine", action="store_true")
    retry = sub.add_parser("requeue-exhausted", help="Retry valid unlabeled snapshot tasks after teacher repair")
    retry.add_argument("--snapshot-version", default="1")
    retry.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    store = module.DecisionDataset(args.database)
    if args.command == "purge":
        print(store.purge(retention_days=args.days))
    elif args.command == "delete-session":
        print(store.delete_session(args.session))
    elif args.command == "export":
        print(store.export(args.output))
    elif args.command == "review":
        count = 0
        for line in Path(args.input).read_text(encoding="utf-8").splitlines():
            if line.strip():
                reviewed = json.loads(line)
                store.update_human_label(reviewed["id"], reviewed["human_label"])
                count += 1
        print(json.dumps({"reviewed": count}))
    elif args.command == "audit-snapshots":
        print(json.dumps(store.audit_snapshots(quarantine=args.quarantine)))
    elif args.command == "requeue-exhausted":
        print(json.dumps({"requeued": store.requeue_exhausted(
            snapshot_version=args.snapshot_version, limit=args.limit)}))
    elif args.command == "audit":
        if not 1 <= args.limit <= 10000:
            parser.error("audit limit must be between 1 and 10000")
        with store.connect() as db:
            requests = [r[0] for r in db.execute(
                "SELECT json_extract(payload,'$.metadata.request_id') AS request FROM samples "
                "WHERE json_valid(payload) AND json_extract(payload,'$.metadata.request_id') IS NOT NULL "
                "GROUP BY request ORDER BY MAX(created) DESC LIMIT ?", (args.limit,))]
        flagged = sum(bool(store.audit_request(request)["flags"]) for request in requests)
        print(json.dumps({"audited_requests": len(requests), "flagged_requests": flagged}))
    else:
        groups = module.split_samples([r for r in store.samples() if r["teacher_label"] is not None and module.snapshot_usable(r)])
        target = Path(args.output)
        target.mkdir(parents=True, exist_ok=True)
        for name, rows in groups.items():
            path = (target / (name + ".jsonl")).resolve()
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            with store.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO derivatives VALUES (?,?)", (str(path), json.dumps([r["id"] for r in rows]))
                )
        print(json.dumps({k: len(v) for k, v in groups.items()}))


if __name__ == "__main__":
    main()
