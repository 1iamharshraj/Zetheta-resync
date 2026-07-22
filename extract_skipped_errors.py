#!/usr/bin/env python3
"""Extract skipped and error records from the resync log file.

The log file is expected to contain plain text log lines written by the gunicorn
error logger, where each [RECORD] line includes status, user_id, course_id,
percentage, and message fields.

Usage:
    python3 extract_skipped_errors.py [log_file]

Outputs:
    skipped_records.csv
    error_records.csv
"""

import csv
import re
import sys
from pathlib import Path
from typing import List


def parse_log_file(log_path: Path) -> tuple[List[dict], List[dict]]:
    skipped: List[dict] = []
    errors: List[dict] = []

    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or "|" not in line:
                continue

            # Log format: "2026-07-22 01:05:29 | INFO | [RECORD] status=..."
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue

            message = parts[2].strip()
            if not message.startswith("[RECORD]"):
                continue

            status_match = re.search(r"status=(\w+)", message)
            if not status_match:
                continue
            status = status_match.group(1)

            user_id_match = re.search(r"user_id=(\d+)", message)
            course_id_match = re.search(r"course_id=(\d+)", message)
            percentage_match = re.search(r"percentage=([^\s]+)", message)
            message_match = re.search(r"message=(.+)", message)

            row = {
                "user_id": user_id_match.group(1) if user_id_match else "",
                "course_id": course_id_match.group(1) if course_id_match else "",
                "percentage": percentage_match.group(1) if percentage_match else "",
                "message": message_match.group(1) if message_match else "",
                "status": status,
            }

            if status == "skipped":
                skipped.append(row)
            elif status == "error":
                errors.append(row)

    return skipped, errors


def write_csv(records: List[dict], path: Path) -> None:
    if not records:
        path.write_text("user_id,course_id,percentage,message\n", encoding="utf-8")
        return

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["user_id", "course_id", "percentage", "message"])
        writer.writeheader()
        writer.writerows(records)


def main() -> int:
    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("logs/error.log")
    if not log_path.exists():
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return 1

    skipped, errors = parse_log_file(log_path)

    write_csv(skipped, Path("skipped_records.csv"))
    write_csv(errors, Path("error_records.csv"))

    print(f"Skipped records: {len(skipped)} -> skipped_records.csv")
    print(f"Error records:   {len(errors)} -> error_records.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
