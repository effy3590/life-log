"""Behavior tests use synthetic notes in temporary directories only."""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="life-log-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "user-data"
        self.script = Path(ledger.__file__).resolve()
        self.sequence = 0

    def cli(self, *arguments, data=None, success=True):
        process = subprocess.run(
            [sys.executable, "-B", str(self.script), "--data-dir", str(data or self.data), *arguments],
            capture_output=True, text=True, timeout=20,
        )
        if success:
            self.assertEqual(process.returncode, 0, process.stderr)
        else:
            self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
            self.assertIn("error", json.loads(process.stderr))
        return process

    def init(self):
        return json.loads(self.cli("init").stdout)

    def note(self, record_id="meal-1", **changes):
        record = {
            "id": record_id,
            "date": "2026-11-21",
            "category": "food",
            "status": "actual",
            "text": "合成测试：午餐一份，份量未称量。",
            "source": "user_report",
            "coverage": "partial",
        }
        record.update(changes)
        return record

    def input_file(self, record):
        self.sequence += 1
        path = self.root / f"input-{self.sequence}.json"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def add(self, record=None):
        return json.loads(self.cli("add", "--input", self.input_file(record or self.note())).stdout)

    def view(self):
        return json.loads(self.cli("list").stdout)

    def raw(self):
        return (self.data / ledger.LOG_NAME).read_bytes()

    def test_init_repeat_preserves_data_and_add_requires_init(self):
        self.cli("add", "--input", self.input_file(self.note()), success=False)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.init()["summary"]["record_count"], 0)
        self.add()
        before = self.raw()
        self.assertEqual(self.init()["summary"]["record_count"], 1)
        self.assertEqual(self.raw(), before)

    def test_add_retry_and_conflicting_id(self):
        self.init()
        original = self.note(details={"quantity": "one bowl", "uncertain": True})
        self.assertEqual(self.add(original)["revision"], 1)
        before = self.raw()
        self.assertEqual(self.add(original)["result"], "unchanged")
        self.assertEqual(self.raw(), before)
        self.cli("add", "--input", self.input_file(self.note(text="不同内容")), success=False)
        self.assertEqual(self.raw(), before)

    def test_generated_ids_are_returned_and_unique(self):
        self.init()
        record = self.note()
        record.pop("id")
        first, second = self.add(record), self.add(record)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.view()["summary"]["record_count"], 2)

    def test_correction_replaces_view_preserves_history_and_is_retry_safe(self):
        self.init()
        original = self.note(details={"old": "removed by full replacement"})
        self.add(original)
        prefix = self.raw()
        replacement = self.note(status="not_done", text="合成更正：没有吃。")
        replacement.pop("id")
        path = self.input_file(replacement)
        result = json.loads(self.cli("correct", "--id", "meal-1", "--input", path).stdout)
        self.assertEqual(result["revision"], 2)
        self.assertTrue(self.raw().startswith(prefix))
        corrected = self.raw()
        self.assertEqual(json.loads(self.cli("correct", "--id", "meal-1", "--input", path).stdout)["result"], "unchanged")
        self.assertEqual(self.raw(), corrected)
        view = self.view()
        self.assertEqual(view["summary"]["record_count"], 1)
        self.assertEqual(view["summary"]["actual_count"], 0)
        self.assertEqual(view["records"][0]["revision"], 2)
        self.assertEqual(view["records"][0]["status"], "not_done")
        self.assertNotIn("details", view["records"][0])
        events = [json.loads(line) for line in self.raw().splitlines()]
        self.assertEqual(events[0]["record"], original)
        self.assertEqual([event["revision"] for event in events], [1, 2])

    def test_unknown_id_mismatch_and_incomplete_correction_do_not_write(self):
        self.init()
        self.add()
        before = self.raw()
        self.cli("correct", "--id", "absent", "--input", self.input_file(self.note("absent")), success=False)
        self.cli("correct", "--id", "meal-1", "--input", self.input_file(self.note("other")), success=False)
        self.cli("correct", "--id", "meal-1", "--input", self.input_file({"text": "partial patch"}), success=False)
        self.assertEqual(self.raw(), before)

    def test_invalid_dates_enums_fields_and_json_do_not_write(self):
        self.init()
        invalid_changes = [
            {"date": "2026-02-30"}, {"date": "20261121"}, {"date": "2026-1-01"},
            {"date": "0000-01-01"}, {"date": "2026-11-21T00:00:00"},
            {"category": "medicine"}, {"status": "unknown"}, {"source": "fact"},
            {"coverage": "missing"}, {"text": "  "}, {"details": []}, {"id": None},
            {"id": "../../escape"}, {"unexpected": 1}, {"details": {"value": float("nan")}},
        ]
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                self.cli("add", "--input", self.input_file(self.note(**changes)), success=False)
                self.assertEqual(self.raw(), b"")
        for content in ['{"date":"2026-11-21","date":"2026-11-20"}', '{"value":1e999}', "{broken"]:
            path = self.root / "invalid.json"
            path.write_text(content, encoding="utf-8")
            self.cli("add", "--input", str(path), success=False)
            self.assertEqual(self.raw(), b"")
        self.add(self.note(date="2024-02-29"))

    def test_planned_purchased_and_missing_are_not_actual_or_invented_days(self):
        self.init()
        for status in ("actual", "planned", "purchased", "not_done"):
            self.add(self.note(status, status=status, coverage="unknown"))
        view = self.view()
        self.assertEqual(view["summary"]["actual_count"], 1)
        self.assertEqual(view["summary"]["record_count"], 4)
        self.assertEqual(view["summary"]["coverage_counts"], {"unknown": 4})
        self.assertEqual({row["date"] for row in view["records"]}, {"2026-11-21"})
        self.assertTrue(all("details" not in row for row in view["records"]))

    def test_corrupted_log_is_rejected_by_every_operation_without_repairing_it(self):
        self.init()
        self.add()
        valid = self.raw()
        bad_revision = json.loads(valid)
        bad_revision["revision"] = 2
        corruptions = [valid[:-1], valid + b"{broken}\n", valid + b"\n", json.dumps(bad_revision).encode() + b"\n", valid + valid]
        source = self.input_file(self.note("new"))
        for index, corrupt in enumerate(corruptions):
            for operation in [("list",), ("init",), ("add", "--input", source),
                              ("export", "--output-dir", str(self.root / f"bad-export-{index}"))]:
                with self.subTest(index=index, operation=operation[0]):
                    (self.data / ledger.LOG_NAME).write_bytes(corrupt)
                    self.cli(*operation, success=False)
                    self.assertEqual(self.raw(), corrupt)
                    self.assertFalse((self.root / f"bad-export-{index}").exists())

    def test_data_directory_must_be_outside_skill_including_symlink_ancestors(self):
        for location in [self.script.parent, self.script.parent / "forbidden-test-data"]:
            self.cli("init", data=location, success=False)
        self.assertFalse((self.script.parent / "forbidden-test-data").exists())
        alias = self.root / "skill-alias"
        try:
            alias.symlink_to(self.script.parent, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic links are unavailable")
        self.cli("init", data=alias / "forbidden-test-data", success=False)
        self.assertFalse((self.script.parent / "forbidden-test-data").exists())

    def test_symbolic_log_is_refused_without_changing_target(self):
        self.init()
        target = self.root / "untouched.jsonl"
        target.write_bytes(b"important content")
        (self.data / ledger.LOG_NAME).unlink()
        try:
            (self.data / ledger.LOG_NAME).symlink_to(target)
        except OSError:
            self.skipTest("Symbolic links are unavailable")
        self.cli("init", success=False)
        self.cli("list", success=False)
        self.cli("add", "--input", self.input_file(self.note()), success=False)
        self.assertEqual(target.read_bytes(), b"important content")

    def test_export_contains_current_views_and_exact_full_history(self):
        self.init()
        self.add(self.note(text="=1+1, synthetic\nsecond line", details={"amount": "unknown"}))
        self.add(self.note("future", status="planned"))
        self.cli("correct", "--id", "future", "--input", self.input_file(self.note("future", status="not_done")))
        output = self.root / "export-one"
        result = json.loads(self.cli("export", "--output-dir", str(output)).stdout)
        self.assertEqual(result["summary"]["actual_count"], 1)
        self.assertEqual((output / ledger.LOG_NAME).read_bytes(), self.raw())
        self.assertEqual(json.loads((output / "current.json").read_text()), self.view())
        rows = list(csv.DictReader(io.StringIO((output / "current.csv").read_text())))
        self.assertEqual(len(rows), 2)
        formula_row = next(row for row in rows if row["id"] == "meal-1")
        self.assertEqual(formula_row["text"], "'=1+1, synthetic\nsecond line")
        self.assertEqual(json.loads(formula_row["details"]), {"amount": "unknown"})
        self.assertEqual(self.cli("list", "--format", "csv").stdout, (output / "current.csv").read_text())
        exported_before = {path.name: path.read_bytes() for path in output.iterdir()}
        self.cli("export", "--output-dir", str(output), success=False)
        self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, exported_before)

    def test_export_refuses_existing_directory_data_and_source_paths(self):
        self.init()
        self.add()
        existing = self.root / "existing"
        existing.mkdir()
        before = self.raw()
        for destination in [existing, self.data, self.data / ledger.LOG_NAME,
                            self.data / "nested-export", self.script, self.script.parent / "forbidden-export"]:
            with self.subTest(destination=destination):
                self.cli("export", "--output-dir", str(destination), success=False)
                self.assertEqual(self.raw(), before)
        self.assertEqual(list(existing.iterdir()), [])
        self.assertFalse((self.script.parent / "forbidden-export").exists())

    def test_failed_atomic_replace_keeps_old_log_and_cleans_temporary_file(self):
        self.init()
        self.add()
        before = self.raw()
        args = ledger.parser().parse_args(["--data-dir", str(self.data), "add", "--input", self.input_file(self.note("next"))])
        with patch.object(ledger.os, "replace", side_effect=OSError("simulated storage failure")):
            with self.assertRaises(OSError):
                ledger.run(args)
        self.assertEqual(self.raw(), before)
        self.assertEqual(list(self.data.glob(".ledger-write-*")), [])

    def test_failed_export_removes_its_incomplete_files(self):
        self.init()
        self.add()
        output = self.root / "failed-export"
        args = ledger.parser().parse_args(["--data-dir", str(self.data), "export", "--output-dir", str(output)])
        original = ledger.atomic_write
        calls = 0

        def fail_second(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated export failure")
            return original(path, content)

        with patch.object(ledger, "atomic_write", side_effect=fail_second):
            with self.assertRaises(OSError):
                ledger.run(args)
        self.assertFalse(output.exists())
        self.assertEqual(self.view()["summary"]["record_count"], 1)

    def test_concurrent_writes_keep_every_record(self):
        self.init()
        processes = []
        for index in range(12):
            source = self.input_file(self.note(f"parallel-{index}"))
            processes.append(subprocess.Popen(
                [sys.executable, "-B", str(self.script), "--data-dir", str(self.data), "add", "--input", source],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.view()["summary"]["record_count"], 12)
        self.assertEqual(len(self.raw().splitlines()), 12)


if __name__ == "__main__":
    unittest.main()
