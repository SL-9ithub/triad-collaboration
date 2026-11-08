"""Dispatch: one INSERT, idempotency, conflict, backup/ACL/transaction failures (T06-T08, T12-T13, T21-T22)."""
from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from unittest import mock

from support import (BridgeCase, automation_rows, backup, bridge_config, dispatch, query_one)


class DispatchTests(BridgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_database()
        self.write_config()
        self.config = bridge_config.load(self.config_path)
        self.task_file()
        self.spec = self.spec(self.config)

    def test_legal_submit_adds_exactly_one_row(self) -> None:
        before = automation_rows(self.database)
        result = dispatch.submit(self.config, self.spec)
        self.assertEqual(result["state"], "registered_once")
        after = automation_rows(self.database)
        self.assertEqual(len(after), len(before) + 1)
        new_id = self.spec["task_id"]
        self.assertEqual([row for row in after if row[0] != new_id], before)
        self.assertEqual(self.row_count("sessions"), 0)
        self.assertEqual(self.row_count("automation_runs"), 0)
        self.assertEqual(result["backup_quick_check"], "ok")
        self.assertTrue(result["backup_acl"]["only_current_user_and_system"])
        self.assertTrue(result["receipt"] and __import__("pathlib").Path(result["receipt"]).is_file())

    def test_t06_duplicate_submit_is_refused_without_new_row(self) -> None:
        dispatch.submit(self.config, self.spec)
        rows = self.row_count()
        with self.assertRaises(ValueError) as caught:
            dispatch.submit(self.config, self.spec)
        self.assertEqual(str(caught.exception), "task_already_registered_use_status")
        self.assertEqual(self.row_count(), rows)

    def test_t06_insert_once_is_idempotent(self) -> None:
        connection = sqlite3.connect(str(self.database), isolation_level=None)
        try:
            first = dispatch.insert_once(connection, self.spec, self.config)
            second = dispatch.insert_once(connection, self.spec, self.config)
        finally:
            connection.close()
        self.assertEqual(first["state"], "registered_once")
        self.assertEqual(second["state"], "already_registered")
        self.assertEqual(self.row_count(), 4)

    def test_t07_same_id_different_content_rejected(self) -> None:
        connection = sqlite3.connect(str(self.database), isolation_level=None)
        try:
            dispatch.insert_once(connection, self.spec, self.config)
            changed = dict(self.spec, name="改名后的同名轮次")
            with self.assertRaises(ValueError) as caught:
                dispatch.insert_once(connection, changed, self.config)
        finally:
            connection.close()
        self.assertEqual(str(caught.exception), "existing_id_content_conflict")
        row = query_one(self.database, "SELECT name FROM automations WHERE id=?", (self.spec["task_id"],))
        self.assertEqual(row[0], "演示第一轮")

    def test_t08_existing_report_rejected(self) -> None:
        (self.project / "第01轮_W报告.md").write_text("已有报告", encoding="utf-8")
        for action in (dispatch.plan, dispatch.submit):
            with self.subTest(action=action.__name__):
                with self.assertRaises(ValueError) as caught:
                    action(self.config, self.spec)
                self.assertEqual(str(caught.exception), "report_already_exists_do_not_replay")
        self.assertEqual(self.row_count(), 3)
        self.assertEqual(self.backup_entries(), [])

    def test_t12_acl_failure_leaves_no_row(self) -> None:
        with mock.patch.object(backup, "restrict_new_directory", side_effect=ValueError("backup_acl_unavailable")):
            with self.assertRaises(ValueError) as caught:
                dispatch.submit(self.config, self.spec)
        self.assertEqual(str(caught.exception), "backup_acl_unavailable")
        self.assertEqual(self.row_count(), 3)
        self.assertEqual(self.row_count("sessions"), 0)

    def test_t12_integrity_failure_leaves_no_row(self) -> None:
        with mock.patch.object(backup, "quick_check", return_value=("corrupt", 3)):
            with self.assertRaises(ValueError) as caught:
                dispatch.submit(self.config, self.spec)
        self.assertEqual(str(caught.exception), "backup_integrity_failed")
        self.assertEqual(self.row_count(), 3)

    def test_t13_transaction_failure_rolls_back(self) -> None:
        before = automation_rows(self.database)
        with mock.patch.object(dispatch, "_insert_row", side_effect=sqlite3.OperationalError("simulated failure")):
            with self.assertRaises(sqlite3.OperationalError):
                dispatch.submit(self.config, self.spec)
        after = automation_rows(self.database)
        self.assertEqual(after, before)
        self.assertEqual(self.row_count("sessions"), 0)
        self.assertEqual(self.row_count("automation_runs"), 0)

    def test_t22_bridge_never_writes_execution_records(self) -> None:
        dispatch.submit(self.config, self.spec)
        dispatch.status(self.config, self.spec)
        self.assertEqual(self.row_count("sessions"), 0)
        self.assertEqual(self.row_count("automation_runs"), 0)

    def test_plan_is_read_only(self) -> None:
        before = self.database.stat().st_mtime_ns
        result = dispatch.plan(self.config, self.spec)
        self.assertEqual(result["state"], "planned")
        self.assertFalse(result["live_database_modified"])
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual(self.database.stat().st_mtime_ns, before)

    def test_status_reports_only_own_definition(self) -> None:
        dispatch.submit(self.config, self.spec)
        result = dispatch.status(self.config, self.spec)
        self.assertTrue(result["registered"])
        self.assertEqual(result["definition"]["recorded_model"], "deepseek-v4.1-flash")
        self.assertNotIn("runs_json", result["definition"])
        self.assertNotIn("prompt", result["definition"])
        self.assertFalse(result["report_exists"])

    def test_default_prompt_asks_for_publish_only(self) -> None:
        self.assertIn("publish", dispatch.DEFAULT_PROMPT)
        self.assertNotIn("notify", dispatch.DEFAULT_PROMPT)

    def test_summary_separates_task_bytes_from_prompt_bytes(self) -> None:
        size = (self.project / "第01轮_C任务单.md").stat().st_size
        planned = dispatch.plan(self.config, self.spec)
        self.assertEqual(planned["task_bytes"], size)
        self.assertGreater(planned["prompt_bytes"], planned["task_bytes"])

    def test_receipt_junction_out_of_project_is_refused_before_any_write(self) -> None:
        import subprocess
        outside = self.root / "receipts-outside"
        outside.mkdir()
        (self.project / ".cw").mkdir()
        link = self.project / ".cw" / "native-dispatch"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                              capture_output=True, text=True, errors="replace")
        if made.returncode != 0 or not link.exists():
            self.skipTest("this account cannot create a junction here: " + (made.stdout or "").strip())
        try:
            with self.assertRaises(ValueError) as caught:
                dispatch.submit(self.config, self.spec)
            self.assertEqual(str(caught.exception), "receipt_path_escapes_project")
            self.assertEqual(self.row_count(), 3)
            self.assertEqual(self.backup_entries(), [], "no backup before a path that escapes")
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            link.rmdir() if link.exists() else None

    def test_concurrent_duplicate_submit_keeps_exactly_one_new_row(self) -> None:
        import threading
        before = automation_rows(self.database)
        outcomes: list[dict] = []

        def insert() -> None:
            connection = sqlite3.connect(str(self.database), timeout=10, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=10000")
            try:
                outcomes.append(dispatch.insert_once(connection, self.spec, self.config))
            except Exception as error:  # noqa: BLE001 - the loser of a race may fail loudly
                outcomes.append({"error": type(error).__name__})
            finally:
                connection.close()

        workers = [threading.Thread(target=insert) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        after = automation_rows(self.database)
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual([row for row in after if row[0] != self.spec["task_id"]], before)
        self.assertEqual([item.get("state") for item in outcomes].count("registered_once"), 1)


class AclTests(BridgeCase):
    def test_acl_readback_restricts_to_user_and_system(self) -> None:
        target = self.root / "private" / "acl-probe"
        target.mkdir()
        backup.restrict_new_directory(target)
        summary = backup.verify_directory_acl(target)
        self.assertTrue(summary["protected"])
        self.assertTrue(summary["only_current_user_and_system"])
        self.assertEqual(summary["principal_count"], 2)

    def test_acl_foreign_principal_is_detected(self) -> None:
        import win32security
        target = self.root / "private" / "acl-probe-bad"
        target.mkdir()
        backup.restrict_new_directory(target)
        descriptor = win32security.GetNamedSecurityInfo(
            str(target), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
        acl = descriptor.GetSecurityDescriptorDacl()
        everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, 0x1F01FF, everyone)
        win32security.SetNamedSecurityInfo(
            str(target), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        with self.assertRaises(ValueError) as caught:
            backup.verify_directory_acl(target)
        self.assertEqual(str(caught.exception), "backup_acl_foreign_principal")


if __name__ == "__main__":
    unittest.main()
