"""Report events: publish / bounded wait / acknowledge.

Reused from `cw_report.py` unchanged in semantics — identity + nonce + report digest
+ event digest binding, atomic exclusive creation, published reports are immutable,
ack never replaces C's judgement. Only the project root now comes from private config.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from . import config as config_module, guards

EVENT_LIMIT = 8192
WAIT_MIN = 1
WAIT_MAX = 55


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def paths(config: dict, project_raw: str, request_id: str, report_name: str,
          *, require_report: bool) -> tuple[Path, Path, Path, Path]:
    """publish/ack need the report now; wait may start before it is written."""
    if not config_module.ID_RE.fullmatch(request_id):
        raise ValueError("Invalid task id")
    project = guards.resolve_project(config, project_raw)
    report = guards.resolve_report(project, report_name, config["limits"], must_exist=require_report)
    events = project / ".cw" / "events"
    receipts = project / ".cw" / "receipts"
    for state in (events, receipts):
        if not config_module.is_within(state.resolve(), project):
            raise ValueError("Path escapes this project")
    return project, report, events / (request_id + ".json"), receipts / (request_id + ".json")


def write_once(path: Path, value: dict) -> str:
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Existing id has different content")
        return "already_exists"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        try:
            temporary.rename(path)
        except FileExistsError:
            if json.loads(path.read_text(encoding="utf-8")) != value:
                raise ValueError("Concurrent submission has different content") from None
            return "already_exists"
    finally:
        temporary.unlink(missing_ok=True)
    return "created"


def read_event(project: Path, report: Path, event_file: Path, request_id: str, nonce: str,
               limits: dict) -> tuple[dict, str]:
    if event_file.stat().st_size > EVENT_LIMIT:
        raise ValueError("Event exceeds size limit")
    event_bytes = event_file.read_bytes()
    event = json.loads(event_bytes)
    expected = {"version": 1, "id": request_id, "project": str(project),
                "report": str(report), "nonce": nonce}
    if any(event.get(key) != value for key, value in expected.items()):
        raise ValueError("Report event does not match the expected task")
    if not report.is_file():
        raise ValueError("Event exists but its report is missing")
    if report.stat().st_size > limits["max_report_bytes"]:
        raise ValueError("Report exceeds size limit")
    content = report.read_bytes()
    if event.get("sha256") != digest(content):
        raise ValueError("Report changed after event publication")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Report is not UTF-8") from None
    if nonce not in text:
        raise ValueError("Verification nonce absent from report")
    return event, digest(event_bytes)


def publish(config: dict, *, project_raw: str, request_id: str, report_name: str, nonce: str) -> dict:
    if not nonce or len(nonce) > config["limits"]["max_nonce_chars"]:
        raise ValueError("Invalid verification nonce")
    project, report, event_file, _ = paths(config, project_raw, request_id, report_name,
                                           require_report=True)
    content = report.read_bytes()
    if len(content) > config["limits"]["max_report_bytes"]:
        raise ValueError("Invalid report content")
    if nonce not in content.decode("utf-8-sig"):
        raise ValueError("Invalid report content")
    event = {"version": 1, "id": request_id, "project": str(project),
             "report": str(report), "nonce": nonce, "sha256": digest(content)}
    return {"ok": True, "state": write_once(event_file, event), "event": str(event_file),
            "event_sha256": digest(event_file.read_bytes())}


def wait(config: dict, *, project_raw: str, request_id: str, report_name: str, nonce: str,
         timeout: int) -> dict:
    """Bounded wait that may start before the worker has written anything."""
    if not WAIT_MIN <= int(timeout) <= WAIT_MAX:
        raise ValueError("Wait must be between 1 and 55 seconds")
    project, report, event_file, receipt_file = paths(
        config, project_raw, request_id, report_name, require_report=False)
    deadline = time.monotonic() + int(timeout)
    while True:
        if event_file.exists():
            event, event_hash = read_event(project, report, event_file, request_id, nonce, config["limits"])
            if receipt_file.exists():
                receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
                if receipt.get("event_sha256") != event_hash:
                    raise ValueError("Acknowledged event was replaced")
                return {"ok": True, "state": "already_acknowledged", "receipt": str(receipt_file),
                        "event_sha256": event_hash}
            return {"ok": True, "state": "report_ready", "event": event, "event_sha256": event_hash}
        if time.monotonic() >= deadline:
            return {"ok": True, "state": "idle", "id": request_id, "waited_seconds": int(timeout)}
        time.sleep(0.5)


def ack(config: dict, *, project_raw: str, request_id: str, report_name: str, nonce: str,
        event_sha256: str, decision: str) -> dict:
    if decision not in ("accepted", "needs_revision"):
        raise ValueError("Invalid decision")
    project, report, event_file, receipt_file = paths(config, project_raw, request_id, report_name,
                                                      require_report=True)
    event, event_hash = read_event(project, report, event_file, request_id, nonce, config["limits"])
    if event_sha256 != event_hash:
        raise ValueError("Acknowledgment does not match the reviewed event")
    receipt = {"id": request_id, "project": str(project), "event_sha256": event_hash,
               "report_sha256": event["sha256"], "decision": decision}
    return {"ok": True, "state": write_once(receipt_file, receipt), "receipt": str(receipt_file),
            "event_sha256": event_hash}
