"""Synthetic fixtures and helpers. No real WorkBuddy data is ever read or copied.

The temporary root is explicit: set TRIAD_BRIDGE_TEST_ROOT to a directory on this
machine that is not a drive root and not inside the repository. Samples are kept
after a run; cleaning them up is a separate, deliberate step (cleanup_test_root.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from triad_bridge import backup, config as bridge_config, dispatch, events, guards  # noqa: E402

PROFILE = TOOLS / "triad_bridge" / "profiles" / "workbuddy-5.9.3-automations.sql"
TEST_ROOT_ENV = "TRIAD_BRIDGE_TEST_ROOT"


def configured_test_root() -> Path:
    raw = os.environ.get(TEST_ROOT_ENV, "").strip()
    if not raw:
        raise unittest.SkipTest(f"set {TEST_ROOT_ENV} to a scratch directory outside the repo")
    base = Path(raw)
    if not base.is_absolute():
        raise ValueError("test_root_must_be_absolute")
    base = base.resolve()
    if base.parent == base:
        raise ValueError("test_root_must_not_be_a_drive_root")
    if base == bridge_config.repo_root().resolve() or bridge_config.repo_root().resolve() in base.parents:
        raise ValueError("test_root_must_be_outside_the_repository")
    base.mkdir(parents=True, exist_ok=True)
    return base


BASE = Path(os.environ.get(TEST_ROOT_ENV) or "<unset TRIAD_BRIDGE_TEST_ROOT>")

OWNER_A = "synthetic-owner-alpha"
OWNER_B = "synthetic-owner-beta"
REF_ONE = str(uuid.uuid5(uuid.NAMESPACE_URL, "triad-tests/reference-one"))
REF_TWO = str(uuid.uuid5(uuid.NAMESPACE_URL, "triad-tests/reference-two"))
OTHER_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "triad-tests/unrelated-row"))

BASE_DDL = PROFILE.read_text(encoding="utf-8")


def ddl_without(column: str) -> str:
    return "\n".join(line for line in BASE_DDL.splitlines()
                     if not line.strip().startswith(column + " "))


def ddl_with_extra() -> str:
    return BASE_DDL.replace("wecom_bot_chat_id TEXT\n);", "wecom_bot_chat_id TEXT,\n    extra_note TEXT\n);")


def ddl_with_type_change() -> str:
    return BASE_DDL.replace("model_is_thinking INTEGER NOT NULL DEFAULT 0",
                            "model_is_thinking TEXT NOT NULL DEFAULT 0")


def ddl_with_default_change() -> str:
    return BASE_DDL.replace("DEFAULT 'recurring'", "DEFAULT 'once'")


def ddl_with_check() -> str:
    return BASE_DDL.replace("name TEXT NOT NULL,", "name TEXT NOT NULL CHECK(length(name) > 0),")


def ddl_with_generated() -> str:
    return BASE_DDL.replace("wecom_bot_chat_id TEXT\n);",
                            "wecom_bot_chat_id TEXT,\n    extra_generated TEXT"
                            " GENERATED ALWAYS AS ('demo') VIRTUAL\n);")


def create_database(path: Path, *, ddl: str | None = None, trigger: bool = False,
                    owners=(OWNER_A, OWNER_A), statuses=("confirmed", "confirmed"),
                    deleted=(None, None), extra_row: bool = True, extra_unique_index: bool = False,
                    plain_index: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(ddl or BASE_DDL)
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE automation_runs (id TEXT PRIMARY KEY)")
        for index, task_id in enumerate((REF_ONE, REF_TWO)):
            connection.execute(
                "INSERT INTO automations (id,name,prompt,status,schedule_type,cwds,rrule,"
                "owner_user_id,owner_status,created_at,updated_at,deleted_at,model_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, "synthetic reference " + str(index + 1), "synthetic prompt", "ACTIVE",
                 "once", "[]", "", owners[index], statuses[index], 1, 1, deleted[index],
                 "deepseek-v4.1-flash"))
        if extra_row:
            connection.execute(
                "INSERT INTO automations (id,name,prompt,status,schedule_type,cwds,rrule,"
                "owner_user_id,owner_status,created_at,updated_at,deleted_at,model_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (OTHER_ID, "synthetic unrelated", "synthetic prompt", "PAUSED", "recurring",
                 "[]", "", OWNER_A, "confirmed", 1, 1, None, "deepseek-v4-pro"))
        if trigger:
            connection.execute(
                "CREATE TRIGGER automations_after_insert AFTER INSERT ON automations"
                " BEGIN UPDATE automations SET updated_at=updated_at WHERE id=NEW.id; END")
        if extra_unique_index:
            connection.execute("CREATE UNIQUE INDEX synthetic_unique_name ON automations(name)")
        if plain_index:
            connection.execute("CREATE INDEX synthetic_lookup_status ON automations(status)")
        connection.commit()
    finally:
        connection.close()
    return path


def snapshot(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        tables = ("automations", "sessions", "automation_runs")
        return {name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in tables}
    finally:
        connection.close()


def automation_rows(path: Path) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT * FROM automations ORDER BY id").fetchall()
    finally:
        connection.close()


def query_one(path: Path, sql: str, params: tuple = ()):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, params).fetchone()
    finally:
        connection.close()


class BridgeCase(unittest.TestCase):
    """Every case works inside its own directory under the configured test root."""

    def setUp(self) -> None:
        self.base = configured_test_root()
        self.root = self.base / ("case-" + uuid.uuid4().hex)
        (self.root / "projects").mkdir(parents=True, exist_ok=False)
        (self.root / "private").mkdir(parents=True, exist_ok=False)
        self.project = self.root / "projects" / "demo project 演示"
        self.project.mkdir(parents=True)
        self.database = self.root / "private" / "workbuddy.db"
        self.backup_dir = self.root / "private" / "backups"
        self.config_path = self.root / "private" / "config.json"

    def tearDown(self) -> None:
        # Samples stay on disk on purpose: removing them is the user's own decision.
        # Every helper here closes its own connection, so no handle outlives a case.
        return None

    # ------------------------------------------------------------------ fixtures

    def make_database(self, **kwargs) -> Path:
        return create_database(self.database, **kwargs)

    def task_file(self, name: str = "第01轮_C任务单.md", text: str = "## 任务\n生成合成结果\n") -> Path:
        path = self.project / name
        path.write_text(text, encoding="utf-8")
        return path

    def raw_config(self, **overrides) -> dict:
        profiles = guards.load_profiles()
        profile = profiles["workbuddy-5.9.3-automations"]
        raw = {
            "version": 1,
            "cooperation_root": str(self.root / "projects"),
            "database_path": str(self.database),
            "backup_dir": str(self.backup_dir),
            "public_source_root": str(bridge_config.repo_root()),
            "namespace_tag": "triad/tests/",
            "profile": {"name": "workbuddy-5.9.3-automations", "fingerprint": profile["fingerprint"]},
            "schema_pin": profile["ddl_sha256"],
            "owner_reference_task_ids": [REF_ONE, REF_TWO],
            "owner_binding_sha256": hashlib.sha256(OWNER_A.encode()).hexdigest(),
            "model_allowlist": ["deepseek-v4.1-flash", "deepseek-v4-pro"],
            "models": {"simple": "deepseek-v4.1-flash", "hard": "deepseek-v4-pro"},
            "limits": dict(bridge_config.DEFAULT_LIMITS),
            "prompt_template": None,
        }
        raw.update(overrides)
        return raw

    def write_config(self, **overrides) -> Path:
        raw = self.raw_config(**overrides)
        validated = bridge_config.validate(raw, config_path=self.config_path)
        bridge_config.write_once(self.config_path, bridge_config.serialise(validated))
        return self.config_path

    def load_config(self, **overrides) -> dict:
        return bridge_config.validate(self.raw_config(**overrides), config_path=self.config_path)

    def spec(self, config: dict, *, request_id: str = "demo-001", name: str = "演示第一轮",
             report: str = "第01轮_W报告.md", nonce: str = "nonce-synthetic-001",
             difficulty: str = "simple", task: str = "第01轮_C任务单.md") -> dict:
        return dispatch.build_spec(
            config, project_raw=str(self.project), request_id=request_id, name=name,
            task_raw=task, report_raw=report, nonce=nonce, difficulty=difficulty,
            difficulty_reason="synthetic fixture, single file change")

    def row_count(self, table: str = "automations") -> int:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()

    def backup_entries(self) -> list[str]:
        return sorted(path.name for path in self.backup_dir.glob("*")) if self.backup_dir.exists() else []


def run_cli(args: list[str], cwd: Path) -> tuple[int, dict]:
    import subprocess
    environment = dict(**{k: v for k, v in __import__("os").environ.items()})
    completed = subprocess.run(
        [sys.executable, "-B", str(TOOLS / "triad_bridge_cli.py")] + args,
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        env=environment, timeout=240)
    payload = {}
    for line in completed.stdout.splitlines():
        if line.strip().startswith("{"):
            payload = json.loads(line)
    return completed.returncode, payload
