"""C-to-W dispatch: build the task spec, plan read-only, submit exactly one definition.

Reused from `native_dispatch.spec_from_args` / `native_once.submit_live` /
`native_once.insert_fixed_trial`, with every author-machine constant replaced by
private configuration. The only live mutation stays a single INSERT into
`automations`; sessions, automation_runs, credentials and permissions are untouched.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import backup, config as config_module, guards

DEFAULT_PROMPT = (
    "合作任务。L是用户，C负责规划与验收，W执行。当前业务目录={project}。\n"
    "C难度评估={difficulty}；依据={difficulty_reason}；本轮指定模型={model}。\n"
    "请求id={request_id}；校验码={nonce}；C任务单SHA256={task_sha256}。\n"
    "指定W报告={report}。本轮只执行下列C任务单；不重放旧任务、不改变权限或鉴权，"
    "遇到权限申请遵守正常流程。\n"
    "所有项目执行时限规则：开始时记录实际时间，每完成一个步骤及每10分钟核对耗时并保存可续接进度。"
    "单轮以30至45分钟为目标；达到45分钟即停止新增工作，整理当前成果、未完成项和下一步。"
    "最迟60分钟内固定指定报告并执行任务单的publish，即使尚未全部完成也必须回报部分完成状态。"
    "长测试拆批，单条工具调用限制在5分钟内并检查结果；预留测试收尾时间。"
    "不得因整项任务未完成而一直工作到90分钟调度超时。C将核验并接续下一轮。"
    "这条分轮交付要求优先于任务单中完成全部工作后才报告的措辞。\n\n"
)

SEMANTIC_FIELDS = ("id", "name", "prompt", "cwds", "workspace_scope", "schedule_type",
                   "model_id", "permission_mode", "owner_user_id", "owner_status",
                   "skills_json", "connector_ids_json", "push_to_wechat", "push_to_wecom_bot")


def build_spec(config: dict, *, project_raw: str, request_id: str, name: str, task_raw: str,
               report_raw: str, nonce: str, difficulty: str, difficulty_reason: str,
               model: str | None = None) -> dict:
    if not config_module.ID_RE.fullmatch(request_id):
        raise ValueError("invalid_request_id")
    reason = (difficulty_reason or "").strip()
    if not 3 <= len(reason) <= 500:
        raise ValueError("difficulty_reason_required")
    project = guards.resolve_project(config, project_raw)
    task_path, task_bytes, task_text = guards.resolve_task(project, task_raw, config["limits"])
    report = guards.resolve_report(project, report_raw, config["limits"])
    chosen = model or config["models"].get(difficulty)
    if not chosen:
        raise ValueError("invalid_difficulty")
    digest = hashlib.sha256(task_bytes).hexdigest()
    identity = config["namespace_tag"] + project.as_posix().casefold() + "/" + request_id
    template = config.get("prompt_template") or DEFAULT_PROMPT
    header = template.format(project=project, difficulty=difficulty, difficulty_reason=reason,
                             model=chosen, request_id=request_id, nonce=nonce,
                             task_sha256=digest, report=report)
    spec = {
        "request_id": request_id,
        "task_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
        "name": name,
        "project": project,
        "task_file": task_path,
        "task_sha256": digest,
        "task_source_bytes": len(task_bytes),
        "report": report,
        "nonce": nonce,
        "difficulty": difficulty,
        "difficulty_reason": reason,
        "model": chosen,
        "prompt": header + task_text,
    }
    return guards.validate_spec(spec, config)


def definition(owner: str, now_ms: int, spec: dict) -> dict:
    due = now_ms + 30_000
    return {
        "id": spec["task_id"], "name": spec["name"], "prompt": spec["prompt"],
        "status": "ACTIVE", "schedule_type": "once", "next_run_at": due,
        "cwds": json.dumps([spec["project"].as_posix()], ensure_ascii=False),
        "workspace_scope": "workspace", "rrule": "",
        "scheduled_at": datetime.fromtimestamp(due / 1000, timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "model_id": spec["model"], "model_is_thinking": 0,
        "skills_json": "[]", "connector_ids_json": "[]",
        "push_to_wechat": 0, "push_to_wecom_bot": 0,
        "owner_user_id": owner, "owner_status": "confirmed", "owner_source": "created",
        "permission_mode": "default", "created_at": now_ms, "updated_at": now_ms,
    }


def _insert_row(connection: sqlite3.Connection, intended: dict) -> None:
    """Single INSERT. Tests patch this to prove rollback leaves no row behind."""
    columns = list(intended)
    marks = ",".join("?" for _ in columns)
    connection.execute("INSERT INTO automations (" + ",".join(columns) + ") VALUES (" + marks + ")",
                       tuple(intended[key] for key in columns))


def insert_once(connection: sqlite3.Connection, spec: dict, config: dict) -> dict:
    guards.schema_guard(connection, config)
    owner = guards.reference_owner(connection, config)
    intended = definition(owner, int(time.time() * 1000), spec)
    columns = list(intended)
    connection.execute("BEGIN IMMEDIATE")
    try:
        guards.schema_guard(connection, config)
        if guards.reference_owner(connection, config) != owner:
            raise ValueError("owner_reference_changed")
        existing = connection.execute(
            "SELECT " + ",".join(columns) + " FROM automations WHERE id=?", (spec["task_id"],)).fetchone()
        if existing:
            old = dict(zip(columns, existing))
            if any(old[key] != intended[key] for key in SEMANTIC_FIELDS):
                raise ValueError("existing_id_content_conflict")
            connection.rollback()
            return {"state": "already_registered", "id": spec["task_id"]}
        _insert_row(connection, intended)
        connection.commit()
        return {"state": "registered_once", "id": spec["task_id"],
                "scheduled_at": intended["scheduled_at"], "next_run_at": intended["next_run_at"],
                "permission_mode": "default"}
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _summary(config: dict, spec: dict) -> dict:
    return {
        "ok": True,
        "request_id": spec["request_id"],
        "task_id": spec["task_id"],
        "project": str(spec["project"]),
        "task_file": str(spec["task_file"]),
        "task_sha256": spec["task_sha256"],
        "task_bytes": spec.get("task_source_bytes", len(spec["prompt"].encode("utf-8"))),
        "prompt_bytes": len(spec["prompt"].encode("utf-8")),
        "report": str(spec["report"]),
        "difficulty": spec["difficulty"],
        "selected_model": spec["model"],
        "permission_mode": "default",
        "note": "requested model != executed model; check the native run record for the model actually used",
    }


def plan(config: dict, spec: dict) -> dict:
    """Read-only preview. Never writes the database, never creates a backup or a config."""
    if spec["report"].exists():
        raise ValueError("report_already_exists_do_not_replay")
    with closing(config_module.readonly(config["database_path"])) as connection:
        schema = guards.schema_guard(connection, config)
        owner = guards.reference_owner(connection, config)
        present = connection.execute("SELECT 1 FROM automations WHERE id=?", (spec["task_id"],)).fetchone()
        rows = connection.execute("SELECT COUNT(*) FROM automations").fetchone()[0]
    result = _summary(config, spec)
    result.update({
        "state": "planned",
        "schema": schema,
        "owner_binding_verified": True,
        "owner_digest_prefix": config["owner_binding_sha256"][:16],
        "already_registered": bool(present),
        "inserts_planned": 0 if present else 1,
        "automations_rows": rows,
        "live_database_modified": False,
        "backup_created": False,
    })
    return result


def receipt_path(project: Path, task_id: str) -> Path:
    """Resolve the receipt location before any write; a junction out of the project is refused."""
    state = project / ".cw" / "native-dispatch"
    if not config_module.is_within(state.resolve(), project):
        raise ValueError("receipt_path_escapes_project")
    return state / (task_id + ".json")


def submit(config: dict, spec: dict) -> dict:
    if spec["report"].exists():
        raise ValueError("report_already_exists_do_not_replay")
    receipt = receipt_path(spec["project"], spec["task_id"])
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if not config_module.is_within(receipt.parent.resolve(), spec["project"]):
        raise ValueError("receipt_path_escapes_project")
    with closing(config_module.readonly(config["database_path"])) as source:
        schema = guards.schema_guard(source, config)
        guards.reference_owner(source, config)
        present = source.execute("SELECT 1 FROM automations WHERE id=?", (spec["task_id"],)).fetchone()
        if present:
            raise ValueError("task_already_registered_use_status")
        rows_before = source.execute("SELECT COUNT(*) FROM automations").fetchone()[0]
        backup_info = backup.create(config, source)
    if backup_info["automations_rows_at_backup"] != rows_before:
        raise ValueError("backup_inconsistent_with_source")
    connection = sqlite3.connect(str(config["database_path"]), timeout=5, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        result = insert_once(connection, spec, config)
    finally:
        connection.close()
    result.update(_summary(config, spec))
    result.update({
        "only_mutation": "INSERT one ACTIVE once definition",
        "execution_records_written_by_bridge": False,
        "credentials_read": False,
        "nonce": spec["nonce"],
        "schema": schema,
        "backup": backup_info["path"],
        "backup_quick_check": backup_info["quick_check"],
        "backup_acl": backup_info["acl"],
        "recorded_at": time.time(),
    })
    receipt = Path(spec["project"]) / ".cw" / "native-dispatch" / (spec["task_id"] + ".json")
    temporary = receipt.with_name(receipt.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(receipt)
    result["receipt"] = str(receipt)
    return result


def status(config: dict, spec: dict) -> dict:
    """Only this task's own registration state; no runs_json, chat bodies or credentials."""
    with closing(config_module.readonly(config["database_path"])) as connection:
        guards.schema_guard(connection, config)
        guards.reference_owner(connection, config)
        row = connection.execute(
            "SELECT status,schedule_type,next_run_at,last_run_at,permission_mode,model_id"
            " FROM automations WHERE id=?", (spec["task_id"],)).fetchone()
    fields = ("status", "schedule_type", "next_run_at", "last_run_at", "permission_mode", "recorded_model")
    result = _summary(config, spec)
    result.update({
        "state": "status",
        "registered": row is not None,
        "definition": dict(zip(fields, row)) if row else None,
        "report_exists": spec["report"].is_file(),
    })
    return result
