"""Schema, account, path and limit guards (T01-T05, T09-T11)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import unittest
from contextlib import closing, redirect_stdout

from support import (BridgeCase, OWNER_A, OWNER_B, REF_ONE, REF_TWO, ddl_with_check,
                     ddl_with_default_change, ddl_with_extra, ddl_with_generated,
                     ddl_with_type_change, ddl_without, guards, bridge_config, dispatch)
from triad_bridge.__main__ import main


class SchemaProfileTests(BridgeCase):
    def test_t01_known_profile_dry_run_writes_nothing(self) -> None:
        self.make_database()
        before = self.database.stat().st_mtime_ns
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--config", str(self.config_path), "init",
                         "--cooperation-root", str(self.root / "projects"),
                         "--database", str(self.database), "--backup-dir", str(self.backup_dir),
                         "--owner-reference", REF_ONE, "--owner-reference", REF_TWO])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["state"], "dry_run")
        self.assertEqual(payload["schema"]["profile"], "workbuddy-5.9.3-automations")
        self.assertEqual(len(payload["schema"]["fingerprint"]), 64)
        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual(self.database.stat().st_mtime_ns, before)
        self.assertEqual(self.row_count(), 3)

    def _reject_init(self, **kwargs) -> dict:
        self.make_database(**kwargs)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--config", str(self.config_path), "init",
                         "--cooperation-root", str(self.root / "projects"),
                         "--database", str(self.database), "--backup-dir", str(self.backup_dir),
                         "--owner-reference", REF_ONE, "--owner-reference", REF_TWO])
        payload = json.loads(buffer.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.backup_dir.exists(), "a rejected schema must not leave a backup dir")
        return payload

    def test_t02_missing_column_rejected(self) -> None:
        payload = self._reject_init(ddl=ddl_without("permission_mode"))
        self.assertEqual(payload["reason"], "unsupported_schema")

    def test_t02_extra_column_rejected(self) -> None:
        self.assertEqual(self._reject_init(ddl=ddl_with_extra())["reason"], "unsupported_schema")

    def test_t02_type_change_rejected(self) -> None:
        self.assertEqual(self._reject_init(ddl=ddl_with_type_change())["reason"], "unsupported_schema")

    def test_t02_default_change_rejected(self) -> None:
        self.assertEqual(self._reject_init(ddl=ddl_with_default_change())["reason"], "unsupported_schema")

    def test_check_constraint_is_not_hidden_by_table_info(self) -> None:
        payload = self._reject_init(ddl=ddl_with_check())
        self.assertEqual(payload["reason"], "schema_ddl_mismatch")

    def test_generated_column_is_not_hidden_by_table_info(self) -> None:
        payload = self._reject_init(ddl=ddl_with_generated())
        self.assertEqual(payload["reason"], "schema_ddl_mismatch")

    def test_extra_unique_index_rejected(self) -> None:
        payload = self._reject_init(extra_unique_index=True)
        self.assertEqual(payload["reason"], "unexpected_unique_index")

    def test_plain_performance_index_is_allowed(self) -> None:
        self.make_database(plain_index=True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--config", str(self.config_path), "init",
                         "--cooperation-root", str(self.root / "projects"),
                         "--database", str(self.database), "--backup-dir", str(self.backup_dir),
                         "--owner-reference", REF_ONE, "--owner-reference", REF_TWO])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["state"], "dry_run")
        self.assertTrue(payload["schema"]["ddl_sha256"])
        self.assertEqual(self.row_count(), 3)

    def test_ddl_tokens_keep_spaces_inside_defaults(self) -> None:
        left = "CREATE TABLE t (a TEXT DEFAULT 'white space')"
        right = "CREATE TABLE t (a TEXT DEFAULT 'whitespace')"
        self.assertNotEqual(guards.ddl_tokens(left), guards.ddl_tokens(right))
        self.assertEqual(guards.ddl_tokens("CREATE  TABLE\n t(a TEXT)"),
                         guards.ddl_tokens("create table t ( a text )"))

    def test_t02_manual_pin_cannot_accept_unknown_schema(self) -> None:
        self.make_database(ddl=ddl_with_extra())
        with closing(bridge_config.readonly(self.database)) as connection:
            live = guards.fingerprint(guards.table_columns(connection))
        config = self.load_config(profile={"name": "workbuddy-5.9.3-automations", "fingerprint": live},
                                  schema_pin=live)
        with closing(bridge_config.readonly(self.database)) as connection:
            with self.assertRaises(ValueError) as caught:
                guards.schema_guard(connection, config)
        self.assertEqual(str(caught.exception), "unsupported_schema")

    def test_unknown_profile_name_rejected(self) -> None:
        self.make_database()
        config = self.load_config(profile={"name": "workbuddy-9.9.9-automations",
                                           "fingerprint": self.raw_config()["profile"]["fingerprint"]})
        with closing(bridge_config.readonly(self.database)) as connection:
            with self.assertRaises(ValueError) as caught:
                guards.schema_guard(connection, config)
        self.assertEqual(str(caught.exception), "unsupported_profile")

    def test_t03_trigger_rejected_before_backup(self) -> None:
        payload = self._reject_init(trigger=True)
        self.assertEqual(payload["reason"], "unexpected_automation_trigger")

    def test_schema_pin_mismatch(self) -> None:
        self.make_database()
        config = self.load_config(schema_pin="0" * 64)
        with closing(bridge_config.readonly(self.database)) as connection:
            with self.assertRaises(ValueError) as caught:
                guards.schema_guard(connection, config)
        self.assertEqual(str(caught.exception), "schema_pin_mismatch")


class AccountBindingTests(BridgeCase):
    def test_t04_single_reference_rejected(self) -> None:
        self.make_database()
        config = self.load_config(owner_reference_task_ids=[REF_ONE, "0" * 8 + "-0000-0000-0000-000000000000"])
        with closing(bridge_config.readonly(self.database)) as connection:
            with self.assertRaises(ValueError) as caught:
                guards.reference_owner(connection, config)
        self.assertEqual(str(caught.exception), "owner_reference_not_verified")

    def test_t04_unconfirmed_or_deleted_rejected(self) -> None:
        for kwargs in ({"statuses": ("confirmed", "pending")}, {"deleted": (None, 1700000000000)}):
            with self.subTest(kwargs=kwargs):
                self.setUp()
                self.make_database(**kwargs)
                config = self.load_config()
                with closing(bridge_config.readonly(self.database)) as connection:
                    with self.assertRaises(ValueError) as caught:
                        guards.reference_owner(connection, config)
                self.assertEqual(str(caught.exception), "owner_reference_not_verified")
                self.tearDown()

    def test_t05_two_owners_rejected(self) -> None:
        self.make_database(owners=(OWNER_A, OWNER_B))
        config = self.load_config()
        with closing(bridge_config.readonly(self.database)) as connection:
            with self.assertRaises(ValueError) as caught:
                guards.reference_owner(connection, config)
        self.assertEqual(str(caught.exception), "owner_reference_conflict")

    def test_both_references_swapped_to_other_owner_rejected(self) -> None:
        self.make_database()
        config = self.load_config()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("UPDATE automations SET owner_user_id=? WHERE id IN (?,?)",
                               (OWNER_B, REF_ONE, REF_TWO))
            connection.commit()
        finally:
            connection.close()
        with closing(bridge_config.readonly(self.database)) as connection:
            with self.assertRaises(ValueError) as caught:
                guards.reference_owner(connection, config)
        self.assertEqual(str(caught.exception), "owner_binding_mismatch")


class PathAndLimitTests(BridgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_database()
        self.config = self.load_config()
        self.task_file()

    def test_t09_project_boundaries(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir()
        for project in (str(self.root / "projects"), str(outside), str(self.project / "nested")):
            with self.subTest(project=project):
                with self.assertRaises(ValueError):
                    dispatch.build_spec(self.config, project_raw=project, request_id="demo-001",
                                        name="n", task_raw=str(self.project / "第01轮_C任务单.md"),
                                        report_raw="r.md", nonce="n1", difficulty="simple",
                                        difficulty_reason="synthetic")

    def test_t09_report_must_stay_in_project_root(self) -> None:
        nested = self.project / "sub"
        nested.mkdir()
        for report in ("sub/报告.md", "../报告.md", "报告.txt"):
            with self.subTest(report=report):
                with self.assertRaises(ValueError):
                    self.spec(self.config, report=report)

    def test_t10_task_boundaries(self) -> None:
        sub = self.project / "sub"
        sub.mkdir()
        (sub / "任务单.md").write_text("x", encoding="utf-8")
        (self.project / "任务单.txt").write_text("x", encoding="utf-8")
        for task in ("sub/任务单.md", "任务单.txt", "缺失.md"):
            with self.subTest(task=task):
                with self.assertRaises(ValueError):
                    self.spec(self.config, task=task)

    def test_t10_task_too_large(self) -> None:
        big = self.project / "大任务单.md"
        big.write_text("x" * (self.config["limits"]["max_task_bytes"] + 1), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.spec(self.config, task="大任务单.md")
        self.assertEqual(str(caught.exception), "task_markdown_too_large")

    def test_t11_unknown_model_rejected(self) -> None:
        config = self.load_config(models={"simple": "deepseek-v4.1-flash", "hard": "deepseek-v4-pro"})
        with self.assertRaises(ValueError):
            dispatch.build_spec(config, project_raw=str(self.project), request_id="demo-001",
                                name="n", task_raw="第01轮_C任务单.md", report_raw="r.md",
                                nonce="n1", difficulty="simple", difficulty_reason="synthetic",
                                model="made-up-model")

    def test_config_paths_outside_public_and_projects(self) -> None:
        with self.assertRaises(ValueError) as caught:
            bridge_config.validate(self.raw_config(backup_dir=str(self.project / "backups")),
                                   config_path=self.config_path)
        self.assertEqual(str(caught.exception), "config_private_path_inside_public_or_projects")

    def test_test_database_is_not_in_public_source(self) -> None:
        self.assertNotIn(str(bridge_config.repo_root()).lower(), str(self.root).lower())
        self.assertTrue(str(self.root).startswith(str(self.base)))


if __name__ == "__main__":
    unittest.main()
