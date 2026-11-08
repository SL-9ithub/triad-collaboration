"""Report events: publishing, bounded wait, acknowledgment (T14-T20, T22)."""
from __future__ import annotations

import json
import time
import unittest

from support import BridgeCase, bridge_config, events

NONCE = "nonce-synthetic-001"
REPORT = "第01轮_W报告.md"


class EventTests(BridgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_database()
        self.write_config()
        self.config = bridge_config.load(self.config_path)
        self.task_file()
        self.report = self.project / REPORT
        self.report.write_text(f"## 理解\n合成报告\n\n校验码={NONCE}\n", encoding="utf-8")

    def publish(self):
        return events.publish(self.config, project_raw=str(self.project), request_id="demo-001",
                              report_name=REPORT, nonce=NONCE)

    def wait(self, timeout: int = 1):
        return events.wait(self.config, project_raw=str(self.project), request_id="demo-001",
                           report_name=REPORT, nonce=NONCE, timeout=timeout)

    def ack(self, sha: str, decision: str = "accepted"):
        return events.ack(self.config, project_raw=str(self.project), request_id="demo-001",
                          report_name=REPORT, nonce=NONCE, event_sha256=sha, decision=decision)

    def test_publish_wait_ack_roundtrip_is_idempotent(self) -> None:
        self.assertEqual(self.publish()["state"], "created")
        ready = self.wait()
        self.assertEqual(ready["state"], "report_ready")
        self.assertEqual(self.ack(ready["event_sha256"])["state"], "created")
        again = self.wait()
        self.assertEqual(again["state"], "already_acknowledged")
        self.assertEqual(self.publish()["state"], "already_exists")
        self.assertEqual(self.row_count("automations"), 3)
        self.assertEqual(self.row_count("sessions"), 0)

    def test_t14_nonce_missing_from_report(self) -> None:
        self.report.write_text("## 报告\n没有校验码\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.publish()
        self.assertEqual(str(caught.exception), "Invalid report content")

    def test_t15_report_changed_after_publish(self) -> None:
        self.publish()
        self.report.write_text(f"## 报告\n{NONCE}\n被改动\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.wait()
        self.assertEqual(str(caught.exception), "Report changed after event publication")

    def test_t16_same_id_different_event_rejected(self) -> None:
        self.publish()
        self.report.write_text(f"## 报告\n{NONCE}\n另一个版本\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.publish()
        self.assertEqual(str(caught.exception), "Existing id has different content")

    def test_t17_wrong_ack_digest_rejected(self) -> None:
        self.publish()
        self.wait()
        with self.assertRaises(ValueError) as caught:
            self.ack("0" * 64)
        self.assertEqual(str(caught.exception), "Acknowledgment does not match the reviewed event")
        self.assertFalse((self.project / ".cw" / "receipts" / "demo-001.json").exists())

    def test_t18_second_ack_is_idempotent(self) -> None:
        self.publish()
        sha = self.wait()["event_sha256"]
        first = self.ack(sha)
        second = self.wait()
        self.assertEqual(first["state"], "created")
        self.assertEqual(second["state"], "already_acknowledged")
        receipt = json.loads((self.project / ".cw" / "receipts" / "demo-001.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["event_sha256"], sha)

    def test_t19_replaced_event_after_ack_rejected(self) -> None:
        self.publish()
        sha = self.wait()["event_sha256"]
        self.ack(sha)
        event_file = self.project / ".cw" / "events" / "demo-001.json"
        payload = json.loads(event_file.read_text(encoding="utf-8"))
        payload["note"] = "replaced"
        event_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.wait()
        self.assertEqual(str(caught.exception), "Acknowledged event was replaced")

    def test_t20_wait_timeout_bounds(self) -> None:
        for timeout in (0, 56):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.wait(timeout=timeout)

    def test_wait_returns_idle_then_ready(self) -> None:
        self.assertEqual(self.wait(timeout=1)["state"], "idle")
        self.publish()
        self.assertEqual(self.wait()["state"], "report_ready")

    def test_wait_may_start_before_the_report_exists(self) -> None:
        self.report.unlink()
        started = time.monotonic()
        result = self.wait(timeout=1)
        self.assertEqual(result["state"], "idle")
        self.assertLess(time.monotonic() - started, 10, "wait must stay bounded")
        self.report.write_text(f"## 理解\n合成报告\n\n校验码={NONCE}\n", encoding="utf-8")
        self.publish()
        self.assertEqual(self.wait()["state"], "report_ready")

    def test_publish_without_report_is_refused(self) -> None:
        self.report.unlink()
        with self.assertRaises(ValueError) as caught:
            self.publish()
        self.assertEqual(str(caught.exception), "report_not_found")

    def test_event_without_its_report_is_an_error_not_idle(self) -> None:
        self.publish()
        self.report.unlink()
        with self.assertRaises(ValueError) as caught:
            self.wait()
        self.assertEqual(str(caught.exception), "Event exists but its report is missing")

    def test_event_requires_report_inside_project(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text(NONCE, encoding="utf-8")
        with self.assertRaises(ValueError):
            events.publish(self.config, project_raw=str(self.project), request_id="demo-001",
                           report_name=str(outside), nonce=NONCE)


if __name__ == "__main__":
    unittest.main()
