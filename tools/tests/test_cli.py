"""CLI: callable from any business cwd, paths with spaces and Chinese (T-CLI)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import cleanup_test_root
from support import BridgeCase, REF_ONE, REF_TWO, bridge_config, run_cli

NONCE = "nonce-synthetic-001"


class CliTests(BridgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_database()
        self.write_config()
        self.config = bridge_config.load(self.config_path)
        self.task_file()
        self.elsewhere = self.root / "other cwd 工作目录"
        self.elsewhere.mkdir()

    def dispatch_args(self, command: str) -> list[str]:
        return [command, "--project", str(self.project), "--id", "demo-001",
                "--name", "演示第一轮", "--task", "第01轮_C任务单.md",
                "--report", "第01轮_W报告.md", "--nonce", NONCE,
                "--difficulty", "simple", "--difficulty-reason", "synthetic fixture single file"]

    def test_plan_and_status_from_unrelated_cwd(self) -> None:
        before = self.database.stat().st_mtime_ns
        code, payload = run_cli(["--config", str(self.config_path)] + self.dispatch_args("plan"),
                               cwd=self.elsewhere)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["state"], "planned")
        self.assertEqual(self.database.stat().st_mtime_ns, before)
        self.assertEqual(list(self.elsewhere.iterdir()), [], "the bridge must not write into the caller cwd")

        code, payload = run_cli(["--config", str(self.config_path)] + self.dispatch_args("status"),
                               cwd=self.elsewhere)
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["registered"])

    def test_discover_is_read_only_and_does_not_choose(self) -> None:
        code, payload = run_cli(["--config", str(self.config_path), "discover", "--limit", "5"],
                               cwd=self.elsewhere)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["count"], 3)
        self.assertTrue(all("owner_digest_prefix" in task for task in payload["tasks"]))
        self.assertIn("not a claim", payload["observed_models_note"])

    def test_error_is_reported_as_json(self) -> None:
        code, payload = run_cli(["--config", str(self.elsewhere / "missing.json")]
                               + self.dispatch_args("status"), cwd=self.elsewhere)
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "config_not_found")

    def test_cli_path_contains_spaces_and_non_ascii(self) -> None:
        entry = Path(__file__).resolve().parents[1] / "triad_bridge_cli.py"
        self.assertIn(" ", str(entry))
        self.assertTrue(entry.is_file())


class CleanupTargetTests(BridgeCase):
    """The cleanup script refuses anything that is not a plain sample directory.

    These cases only exercise refusal and listing. No case removes a directory
    recursively, so a mistake here cannot delete somebody's data.
    """

    def setUp(self) -> None:
        super().setUp()
        self.base = self.root / "samples"
        self.base.mkdir()
        self.outside = self.root / "private"
        (self.outside / "keep.txt").write_text("keep", encoding="utf-8")

    def make_junction(self, name: str) -> Path:
        link = self.base / name
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.outside)],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=60)
        if completed.returncode != 0:
            self.skipTest("junction creation unavailable here: " + (completed.stderr or completed.stdout).strip())
        return link

    def make_symlink(self, name: str) -> Path:
        link = self.base / name
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlink unavailable here: {error}")
        return link

    def run_listing(self) -> tuple[int, str]:
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {cleanup_test_root.ENV: str(self.base)}), \
                contextlib.redirect_stdout(buffer):
            return cleanup_test_root.main([]), buffer.getvalue()

    def test_plain_sample_is_accepted(self) -> None:
        sample = self.base / "case-plain"
        sample.mkdir()
        resolved, _ = cleanup_test_root.verify_target(self.base, sample)
        self.assertTrue(os.path.samefile(resolved, sample))

    def test_junction_sample_is_refused(self) -> None:
        link = self.make_junction("case-junction")
        with self.assertRaises(cleanup_test_root.UnsafeTarget) as caught:
            cleanup_test_root.verify_target(self.base, link)
        self.assertEqual(caught.exception.reason, "link_or_reparse_point")

    def test_symlink_sample_is_refused(self) -> None:
        link = self.make_symlink("case-link")
        with self.assertRaises(cleanup_test_root.UnsafeTarget) as caught:
            cleanup_test_root.verify_target(self.base, link)
        self.assertEqual(caught.exception.reason, "link_or_reparse_point")

    def test_nested_sample_is_refused(self) -> None:
        nested = self.base / "case-outer" / "case-inner"
        nested.mkdir(parents=True)
        with self.assertRaises(cleanup_test_root.UnsafeTarget) as caught:
            cleanup_test_root.verify_target(self.base, nested)
        self.assertEqual(caught.exception.reason, "not_a_direct_child_of_the_selected_root")

    def test_listing_reports_the_junction_and_removes_nothing(self) -> None:
        sample = self.base / "case-plain"
        sample.mkdir()
        (sample / "sample.txt").write_text("keep", encoding="utf-8")
        link = self.make_junction("case-junction")

        code, output = self.run_listing()

        self.assertEqual(code, 2, output)
        self.assertIn("link_or_reparse_point", output)
        self.assertIn("list only", output)
        self.assertTrue((sample / "sample.txt").read_text(encoding="utf-8") == "keep")
        self.assertTrue(link.is_dir(), "listing must not follow or delete a junction")
        self.assertTrue((self.outside / "keep.txt").is_file())


class NewUserFlowTests(BridgeCase):
    """A user who has no config yet: discover the two reference tasks, then bind."""

    def setUp(self) -> None:
        super().setUp()
        self.make_database()
        self.task_file()
        self.elsewhere = self.root / "start here 起始目录"
        self.elsewhere.mkdir()

    def init_args(self, *, write: bool) -> list[str]:
        args = ["--config", str(self.config_path), "init",
                "--cooperation-root", str(self.root / "projects"),
                "--database", str(self.database), "--backup-dir", str(self.backup_dir),
                "--owner-reference", REF_ONE, "--owner-reference", REF_TWO]
        return args + (["--write"] if write else [])

    def dispatch_args(self, command: str) -> list[str]:
        return [command, "--project", str(self.project), "--id", "demo-001",
                "--name", "演示第一轮", "--task", "第01轮_C任务单.md",
                "--report", "第01轮_W报告.md", "--nonce", NONCE,
                "--difficulty", "simple", "--difficulty-reason", "synthetic fixture single file"]

    def test_discover_unbound_then_init_then_plan(self) -> None:
        code, found = run_cli(["discover", "--database", str(self.database), "--limit", "5"],
                              cwd=self.elsewhere)
        self.assertEqual(code, 0, found)
        self.assertEqual(found["mode"], "unbound_database")
        self.assertEqual(found["count"], 3)
        self.assertIn(REF_ONE, [task["id"] for task in found["tasks"]])
        self.assertNotIn("prompt", found["tasks"][0])
        self.assertFalse(self.config_path.exists(), "discovery must not create a config")

        code, payload = run_cli(self.init_args(write=True), cwd=self.elsewhere)
        self.assertEqual(code, 0, payload)
        self.assertTrue(self.config_path.exists())
        self.assertEqual(payload["models"]["hard"], "deepseek-v4.1-flash")

        code, planned = run_cli(["--config", str(self.config_path)] + self.dispatch_args("plan"),
                                cwd=self.elsewhere)
        self.assertEqual(code, 0, planned)
        self.assertEqual(planned["state"], "planned")
        self.assertEqual(self.row_count(), 3)

    def test_discover_needs_exactly_one_input(self) -> None:
        self.write_config()
        both = run_cli(["--config", str(self.config_path), "discover",
                        "--database", str(self.database)], cwd=self.elsewhere)
        self.assertNotEqual(both[0], 0)
        self.assertEqual(both[1]["reason"], "discover_ambiguous_input")
        neither = run_cli(["discover"], cwd=self.elsewhere)
        self.assertNotEqual(neither[0], 0)
        self.assertEqual(neither[1]["reason"], "discover_requires_config_or_database")

    def test_commands_other_than_discover_still_need_a_config(self) -> None:
        code, payload = run_cli(self.dispatch_args("status"), cwd=self.elsewhere)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["reason"], "config_required")

    def test_init_refuses_a_non_empty_backup_directory_before_any_write(self) -> None:
        self.backup_dir.mkdir(parents=True)
        keeper = self.backup_dir / "someone-elses-file.txt"
        keeper.write_text("do not touch", encoding="utf-8")
        code, payload = run_cli(self.init_args(write=True), cwd=self.elsewhere)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["reason"], "backup_dir_not_empty_use_new_directory")
        self.assertFalse(self.config_path.exists())
        self.assertEqual(keeper.read_text(encoding="utf-8"), "do not touch")

    def test_init_refuses_conflicting_config_before_touching_permissions(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text('{"version": 1}', encoding="utf-8")
        code, payload = run_cli(self.init_args(write=True), cwd=self.elsewhere)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["reason"], "config_exists_with_different_content")
        self.assertFalse(self.backup_dir.exists())

    def test_repeated_init_is_read_only(self) -> None:
        self.assertEqual(run_cli(self.init_args(write=True), cwd=self.elsewhere)[0], 0)
        before = self.config_path.stat().st_mtime_ns
        sample = self.backup_dir / "sample.txt"
        sample.write_text("kept", encoding="utf-8")
        code, payload = run_cli(self.init_args(write=True), cwd=self.elsewhere)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["config_state"], "already_exists")
        self.assertFalse(payload["acl_applied"])
        self.assertEqual(self.config_path.stat().st_mtime_ns, before)
        self.assertEqual(sample.read_text(encoding="utf-8"), "kept")


if __name__ == "__main__":
    unittest.main()
