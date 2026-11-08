"""Single CLI: init / discover / plan / submit / status / publish / wait / ack.

`python <install>/tools/triad_bridge_cli.py --config <private config> <command> ...`
works from any business cwd; no PYTHONPATH or PATH changes are required.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from pathlib import Path

from . import backup, config as config_module, dispatch, events, guards


def _fail(error: Exception) -> int:
    reason = str(error) if isinstance(error, ValueError) else "bridge_operation_failed"
    print(json.dumps({"ok": False, "error_class": type(error).__name__, "reason": reason},
                     ensure_ascii=True), flush=True)
    return 1


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=True), flush=True)
    return 0


# --------------------------------------------------------------------------- init


def _probe(connection, references) -> tuple[dict, str, list[dict]]:
    """Same inspection as schema_guard: full stored DDL, columns, unique indexes, triggers."""
    schema = guards.match_profile(guards.inspect(connection))
    rows = connection.execute(
        "SELECT id,owner_user_id,owner_status,deleted_at FROM automations WHERE id IN (?,?)",
        tuple(references)).fetchall()
    if len(rows) != 2 or {str(row[0]) for row in rows} != set(references):
        raise ValueError("owner_reference_not_verified")
    owners = set()
    for row in rows:
        if not row[1] or row[2] != "confirmed" or row[3] is not None:
            raise ValueError("owner_reference_not_verified")
        owners.add(row[1])
    if len(owners) != 1:
        raise ValueError("owner_reference_conflict")
    return schema, owners.pop(), rows


def cmd_init(args) -> int:
    if not args.config:
        raise ValueError("config_required")
    config_path = Path(args.config)
    database = Path(args.database)
    if not database.is_absolute() or not database.is_file():
        raise ValueError("config_path_missing:database_path")
    references = [str(value) for value in (args.owner_reference or [])]
    if len(references) != 2:
        raise ValueError("config_requires_two_reference_tasks")
    with closing(config_module.readonly(database.resolve())) as connection:
        schema, owner, _rows = _probe(connection, references)
        if args.profile and args.profile != schema["profile"]:
            raise ValueError("unsupported_schema")
    binding = guards.owner_digest(owner)
    simple = args.model_simple
    hard = args.model_hard or simple
    raw = {
        "version": 1,
        "cooperation_root": args.cooperation_root,
        "database_path": str(database),
        "backup_dir": args.backup_dir,
        "public_source_root": str(config_module.repo_root()),
        "namespace_tag": args.namespace_tag,
        "profile": {"name": schema["profile"], "fingerprint": schema["fingerprint"]},
        "schema_pin": schema["ddl_sha256"],
        "owner_reference_task_ids": references,
        "owner_binding_sha256": binding,
        "model_allowlist": sorted({simple, hard}),
        "models": {"simple": simple, "hard": hard},
        "limits": dict(config_module.DEFAULT_LIMITS),
        "prompt_template": None,
    }
    validated = config_module.validate(raw, config_path=config_path)
    serialised = config_module.serialise(validated)
    backup_dir = validated["backup_dir"]

    # Both checks are read-only and happen before any ACL or config write.
    existing_config = None
    if config_path.exists():
        if json.loads(config_path.read_text(encoding="utf-8-sig")) != serialised:
            raise ValueError("config_exists_with_different_content")
        existing_config = "identical"
    if backup_dir.exists():
        if not backup_dir.is_dir():
            raise ValueError("backup_dir_not_a_directory")
        entries = list(backup_dir.iterdir())
        if entries and existing_config is None:
            raise ValueError("backup_dir_not_empty_use_new_directory")
        directory_state = "existing_non_empty" if entries else "existing_empty"
        creatable = True
    else:
        directory_state = "absent"
        parent = backup_dir.parent
        creatable = parent.is_dir() and os.access(parent, os.W_OK)

    summary = {
        "ok": True,
        "command": "init",
        "state": "written" if args.write else "dry_run",
        "config_path": str(config_path),
        "config_inside_public_source": False,
        "schema": {"profile": schema["profile"], "fingerprint": schema["fingerprint"],
                   "ddl_sha256": schema["ddl_sha256"], "columns": schema["columns"],
                   "trigger_present": False},
        "profile_registry": guards.describe_profiles(),
        "owner_binding_prefix": binding[:16],
        "owner_reference_count": 2,
        "models": raw["models"],
        "backup_dir_state": directory_state,
        "backup_dir_creatable": creatable,
        "writes_database": False,
        "wrote_config": False,
    }
    if not args.write:
        existing = "this path already holds the same config" if existing_config else "nothing exists yet"
        summary["note"] = f"dry run: {existing}; re-run with --write to bind this machine"
        return _emit(summary)

    if existing_config == "identical":
        summary["config_state"] = "already_exists"
        summary["acl_applied"] = False
        summary["backup_dir_acl"] = backup.verify_directory_acl(backup_dir)
        summary["note"] = "config already present and identical; directory permissions left untouched"
        return _emit(summary)
    if directory_state == "absent":
        backup_dir.mkdir(parents=True, exist_ok=True)
    backup.restrict_new_directory(backup_dir)
    summary["acl_applied"] = True
    summary["backup_dir_acl"] = backup.verify_directory_acl(backup_dir)
    summary["config_state"] = config_module.write_once(config_path, serialised)
    summary["wrote_config"] = summary["config_state"] != "already_exists"
    return _emit(summary)


# --------------------------------------------------------------------------- discover


def _discover_query(connection, limit: int):
    rows = connection.execute(
        "SELECT id,name,status,schedule_type,owner_user_id,owner_status,deleted_at,model_id,created_at"
        " FROM automations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    models = connection.execute(
        "SELECT DISTINCT model_id FROM automations WHERE model_id IS NOT NULL ORDER BY model_id").fetchall()
    return rows, models


def cmd_discover(args) -> int:
    """Two modes: unbound (--database) before init, bound (--config) after it."""
    limit = max(1, min(int(args.limit), 50))
    has_config = bool(args.config)
    has_database = bool(getattr(args, "database", None))
    if has_config and has_database:
        raise ValueError("discover_ambiguous_input")
    if not has_config and not has_database:
        raise ValueError("discover_requires_config_or_database")

    if has_database:
        database = Path(args.database)
        if not database.is_absolute() or not database.is_file():
            raise ValueError("config_path_missing:database_path")
        with closing(config_module.readonly(database.resolve())) as connection:
            schema = guards.match_profile(guards.inspect(connection), args.profile)
            rows, models = _discover_query(connection, limit)
        bound, binding, mode = False, None, "unbound_database"
    else:
        config = config_module.load(Path(args.config))
        with closing(config_module.readonly(config["database_path"])) as connection:
            guards.schema_guard(connection, config)
            rows, models = _discover_query(connection, limit)
        schema = {"profile": config["profile"]["name"], "fingerprint": config["profile"]["fingerprint"]}
        bound, binding, mode = True, config["owner_binding_sha256"], "bound_config"

    tasks = []
    for row in rows:
        owner = row[4] or ""
        tasks.append({
            "id": row[0], "name": row[1], "status": row[2], "schedule_type": row[3],
            "owner_digest_prefix": guards.owner_digest(owner)[:16] if owner else None,
            "owner_status": row[5], "deleted": row[6] is not None,
            "observed_model": row[7], "created_at": row[8],
            "matches_bound_owner": bool(binding) and bool(owner)
            and guards.owner_digest(owner) == binding,
        })
    return _emit({
        "ok": True,
        "command": "discover",
        "mode": mode,
        "read_only": True,
        "schema": {"profile": schema["profile"], "fingerprint": schema["fingerprint"]},
        "limit": limit,
        "count": len(tasks),
        "tasks": tasks,
        "observed_models": [row[0] for row in models],
        "observed_models_note": "models seen in past rows only; this is not a claim that any of them is "
                                "currently available, and the bridge never auto-selects the newest one",
        "selection_note": "pick two tasks you created yourself (same account, confirmed, not deleted); "
                          "init does not auto-select tasks or models",
    })


# --------------------------------------------------------------------------- dispatch


def _spec(args, config):
    return dispatch.build_spec(
        config, project_raw=args.project, request_id=args.id, name=args.name,
        task_raw=args.task, report_raw=args.report, nonce=args.nonce,
        difficulty=args.difficulty, difficulty_reason=args.difficulty_reason,
        model=args.model)


def _require_config(args) -> dict:
    if not getattr(args, "config", None):
        raise ValueError("config_required")
    return config_module.load(Path(args.config))


def cmd_dispatch(args) -> int:
    config = _require_config(args)
    spec = _spec(args, config)
    if args.command == "plan":
        return _emit(dispatch.plan(config, spec))
    if args.command == "submit":
        return _emit(dispatch.submit(config, spec))
    return _emit(dispatch.status(config, spec))


def cmd_events(args) -> int:
    config = _require_config(args)
    common = {"project_raw": args.project, "request_id": args.id,
              "report_name": args.report, "nonce": args.nonce}
    if args.command == "publish":
        result = events.publish(config, **common)
    elif args.command == "wait":
        result = events.wait(config, timeout=args.timeout, **common)
    else:
        result = events.ack(config, event_sha256=args.event_sha256 or "",
                            decision=args.decision, **common)
    result["command"] = args.command
    return _emit(result)


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triad_bridge",
        description="Configurable adapter over the native W scheduler and report events.")
    parser.add_argument("--config", default=None,
                        help="private config outside this repo; init writes it, other commands read it")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="bind this machine once (dry-run by default)")
    init.add_argument("--cooperation-root", required=True)
    init.add_argument("--database", required=True)
    init.add_argument("--backup-dir", required=True)
    init.add_argument("--owner-reference", action="append", default=[],
                      help="task id you created yourself; give exactly two")
    init.add_argument("--model-simple", default="deepseek-v4.1-flash")
    init.add_argument("--model-hard", default=None,
                      help="defaults to --model-simple; pass a stronger model only on purpose")
    init.add_argument("--namespace-tag", default="triad/native/")
    init.add_argument("--profile", default=None, help="expect a specific reviewed profile name")
    init.add_argument("--write", action="store_true", help="write the config (default: dry run)")
    init.set_defaults(handler=cmd_init)

    discover = sub.add_parser("discover", help="read-only listing of tasks and observed models")
    discover.add_argument("--limit", type=int, default=20)
    discover.add_argument("--database", default=None,
                          help="read-only mode before init: an explicit WorkBuddy database file")
    discover.add_argument("--profile", default=None,
                          help="with --database: expect this reviewed profile name")
    discover.set_defaults(handler=cmd_discover)

    for name in ("plan", "submit", "status"):
        command = sub.add_parser(name)
        command.add_argument("--project", required=True)
        command.add_argument("--id", required=True)
        command.add_argument("--name", required=True)
        command.add_argument("--task", required=True)
        command.add_argument("--report", required=True)
        command.add_argument("--nonce", required=True)
        command.add_argument("--difficulty", choices=["simple", "hard"], required=True)
        command.add_argument("--difficulty-reason", required=True)
        command.add_argument("--model", default=None)
        command.set_defaults(handler=cmd_dispatch)

    for name in ("publish", "wait", "ack"):
        command = sub.add_parser(name)
        command.add_argument("--project", required=True)
        command.add_argument("--id", required=True)
        command.add_argument("--report", required=True)
        command.add_argument("--nonce", required=True)
        if name == "wait":
            command.add_argument("--timeout", type=int, default=45)
        if name == "ack":
            command.add_argument("--event-sha256", default=None)
            command.add_argument("--decision", choices=["accepted", "needs_revision"], default="accepted")
        command.set_defaults(handler=cmd_events)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except Exception as error:  # noqa: BLE001 - every failure is reported as JSON
        return _fail(error)


if __name__ == "__main__":
    sys.exit(main())
