#!/usr/bin/env python3
"""Local, append-only life notes. Python 3.10+, standard library only."""

import argparse
import csv
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent
LOG_NAME = "events.jsonl"
FIELDS = {"date", "category", "status", "text", "source", "coverage"}
CHOICES = {
    "category": {"food", "sleep", "activity", "feeling", "note"},
    "status": {"actual", "planned", "purchased", "not_done"},
    "source": {"user_report", "device_estimate", "ai_estimate"},
    "coverage": {"partial", "complete", "unknown"},
}
EVENT_FIELDS = {"schema_version", "event_id", "action", "id", "revision", "recorded_at", "record"}
CSV_FIELDS = ["id", "revision", "updated_at", "date", "category", "status", "text", "source", "coverage", "details"]


class LedgerError(Exception):
    pass


def fail(message):
    raise LedgerError(message)


def is_inside(path, directory):
    return path == directory or directory in path.parents


def external_path(value, label):
    original = Path(value).expanduser()
    if original.is_symlink():
        fail(f"{label} must not itself be a symbolic link")
    path = original.resolve()
    if is_inside(path, SKILL_DIR):
        fail(f"{label} must be outside the skill directory")
    return path


def regular_or_missing(path):
    if path.is_symlink():
        fail(f"Refusing symbolic link: {path.name}")
    if path.exists() and not path.is_file():
        fail(f"Expected a regular file: {path.name}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def decode_json(text):
    def invalid_constant(value):
        fail(f"Non-finite JSON number is not allowed: {value}")

    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        # Reject overflow such as 1e999, too, rather than writing non-standard JSON.
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        fail(f"Invalid JSON: {exc}")


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        fail("id must be 1-80 ASCII letters, numbers, dots, underscores or hyphens, starting with a letter or number")
    return value


def validate_record(value, record_id=None):
    if not isinstance(value, dict):
        fail("Input must be a JSON object")
    if set(value) - (FIELDS | {"id", "details"}):
        fail("Input contains unknown fields")
    missing = FIELDS - set(value)
    if missing:
        fail("Missing required fields: " + ", ".join(sorted(missing)))
    chosen_id = value.get("id", record_id)
    if "id" in value or chosen_id is not None:
        valid_id(chosen_id)
    if record_id is not None and chosen_id != record_id:
        fail("Input id must match --id")
    value_date = value["date"]
    if not isinstance(value_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value_date):
        fail("date must be an ISO calendar date: YYYY-MM-DD")
    try:
        date.fromisoformat(value_date)
    except ValueError:
        fail("date is not a valid calendar date")
    for field, choices in CHOICES.items():
        if not isinstance(value[field], str) or value[field] not in choices:
            fail(f"Invalid {field}; expected one of: {', '.join(sorted(choices))}")
    if not isinstance(value["text"], str) or not value["text"].strip():
        fail("text must be a non-empty string")
    if "details" in value and not isinstance(value["details"], dict):
        fail("details must be a JSON object")
    result = dict(value)
    result["id"] = chosen_id or uuid.uuid4().hex
    return result


def read_input(path):
    return decode_json(Path(path).expanduser().read_text(encoding="utf-8"))


@contextmanager
def locked(data_dir):
    """OS advisory lock: automatically released after normal exit or process death."""
    path = data_dir / ".ledger.lock"
    regular_or_missing(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        fail("Lock must be a regular file")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            deadline = time.monotonic() + 10
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        fail("Ledger is busy; retry later")
                    time.sleep(0.05)
        else:
            import fcntl

            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        fail("Ledger is busy; retry later")
                    time.sleep(0.05)
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(path, content):
    regular_or_missing(path)
    fd, name = tempfile.mkstemp(prefix=".ledger-write-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        regular_or_missing(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_log(path):
    regular_or_missing(path)
    if not path.exists():
        fail("Ledger is not initialized; run init first")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        fail("Damaged event log: final newline is missing; no data was changed")
    current = {}
    event_ids = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            event = decode_json(line.decode("utf-8"))
            if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
                fail("Invalid event fields")
            if type(event["schema_version"]) is not int or event["schema_version"] != 1:
                fail("Unsupported event schema")
            event_id = valid_id(event["event_id"])
            if event_id in event_ids:
                fail("Duplicate event_id")
            event_ids.add(event_id)
            record_id = valid_id(event["id"])
            if not isinstance(event["record"], dict) or event["record"].get("id") != record_id:
                fail("Event record id is missing or inconsistent")
            record = validate_record(event["record"], record_id)
            revision = event["revision"]
            if type(revision) is not int or revision < 1:
                fail("Invalid revision")
            timestamp = event["recorded_at"]
            if not isinstance(timestamp, str):
                fail("Invalid event timestamp")
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                fail("Event timestamp needs a timezone")
            previous = current.get(record_id)
            if event["action"] == "add":
                if previous or revision != 1:
                    fail("Repeated add or invalid first revision")
            elif event["action"] == "correct":
                if not previous or revision != previous["revision"] + 1:
                    fail("Correction has no original or has an invalid revision")
            else:
                fail("Unknown event action")
            current[record_id] = {"record": record, "revision": revision, "updated_at": timestamp}
        except (LedgerError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            fail(f"Damaged event log at line {line_number}: {exc}; no data was changed")
    return raw, current


def snapshot(current):
    records = [dict(item["record"], revision=item["revision"], updated_at=item["updated_at"])
               for item in current.values()]
    records.sort(key=lambda item: (item["date"], item["id"]))
    statuses = Counter(item["status"] for item in records)
    return {
        "schema_version": 1,
        "summary": {
            "record_count": len(records),
            "actual_count": statuses.get("actual", 0),
            "status_counts": dict(sorted(statuses.items())),
            "coverage_counts": dict(sorted(Counter(item["coverage"] for item in records).items())),
        },
        "records": records,
    }


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def csv_text(records):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        row = {key: record.get(key, "") for key in CSV_FIELDS}
        row["details"] = json.dumps(record["details"], ensure_ascii=False, allow_nan=False) if "details" in record else ""
        # Spreadsheet software must not interpret user-supplied text as a formula.
        for key, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                row[key] = "'" + value
        writer.writerow(row)
    return stream.getvalue()


def export_bundle(output_dir, data_dir, raw, current):
    destination = external_path(output_dir, "output directory")
    if is_inside(destination, data_dir):
        fail("Output directory must be outside the data directory")
    if destination.exists():
        fail("Output directory already exists; choose a new directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive mkdir prevents overwriting even when two exports race.
    destination.mkdir(mode=0o700)
    written = []
    try:
        view = snapshot(current)
        outputs = {
            "current.json": json_bytes(view),
            "current.csv": csv_text(view["records"]).encode("utf-8"),
            LOG_NAME: raw,
        }
        for name, content in outputs.items():
            target = destination / name
            atomic_write(target, content)
            written.append(target)
    except BaseException:
        for target in written:
            target.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
    return {"result": "exported", "output_dir": str(destination), "files": list(outputs), "summary": view["summary"]}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--data-dir", required=True, help="User data directory outside this skill")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create an empty ledger; safe to repeat")
    add = commands.add_parser("add", help="Add one record from a JSON file")
    add.add_argument("--input", required=True)
    correct = commands.add_parser("correct", help="Replace a record and preserve its history")
    correct.add_argument("--id", required=True)
    correct.add_argument("--input", required=True)
    listing = commands.add_parser("list", help="Print current records and counts")
    listing.add_argument("--format", choices=["json", "csv"], default="json")
    export = commands.add_parser("export", help="Export JSON, CSV and history to a new directory")
    export.add_argument("--output-dir", required=True)
    return result


def run(args):
    data_dir = external_path(args.data_dir, "data directory")
    if args.command == "init":
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not data_dir.is_dir():
        fail("Data directory does not exist; run init first")
    # Validate user input before touching the event log.
    incoming = None
    if args.command in {"add", "correct"}:
        record_id = valid_id(args.id) if args.command == "correct" else None
        incoming = validate_record(read_input(args.input), record_id)
    path = data_dir / LOG_NAME
    with locked(data_dir):
        if args.command == "init" and not path.exists():
            atomic_write(path, b"")
        raw, current = load_log(path)
        if args.command == "init":
            return {"result": "initialized", "data_dir": str(data_dir), "summary": snapshot(current)["summary"]}
        if args.command == "list":
            view = snapshot(current)
            return csv_text(view["records"]) if args.format == "csv" else view
        if args.command == "export":
            return export_bundle(args.output_dir, data_dir, raw, current)
        record_id = incoming["id"]
        previous = current.get(record_id)
        if args.command == "correct" and previous is None:
            fail("Unknown id; a correction requires an existing record")
        if previous is not None and incoming == previous["record"]:
            return {"result": "unchanged", "id": record_id, "revision": previous["revision"]}
        if args.command == "add" and previous is not None:
            fail("id already exists with different content; use correct")
        revision = previous["revision"] + 1 if previous else 1
        event = {
            "schema_version": 1,
            "event_id": uuid.uuid4().hex,
            "action": args.command,
            "id": record_id,
            "revision": revision,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "record": incoming,
        }
        line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        atomic_write(path, raw + line.encode("utf-8"))
        return {"result": "added" if args.command == "add" else "corrected", "id": record_id, "revision": revision}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        if isinstance(result, str):
            sys.stdout.write(result)
        else:
            sys.stdout.write(json_bytes(result).decode("utf-8"))
        return 0
    except (LedgerError, OSError, UnicodeError, ValueError, RecursionError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
