#!/usr/bin/env python3
"""
Resync Zetheta submissions by fetching tech and non-tech submission lists,
constructing per-user report URIs, fetching the report JSON for scores,
and calling the update_submissions API for each record.
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TECH_SUBMISSIONS_URL = "https://www.zetheta.com/wp-json/v1/submissions"
NONTECH_SUBMISSIONS_URL = "https://www.zetheta.com/wp-json/v1/submissions/?type=nontech"
UPDATE_API_URL = "https://www.zetheta.com/wp-json/v1/update_submissions/"

TECH_REPORT_BASE = "https://zetheta-reports.s3.ap-south-1.amazonaws.com/reports/{user_id}/{course_number}_{course_name_underscore}_result.json"
NONTECH_REPORT_BASE = (
    "https://zetheta-reports.s3.ap-south-1.amazonaws.com/non-tech-reports/{user_id}/"
    "report_{user_id}_{course_number}_{course_name_underscore}.json"
)

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ZethetaResync/1.0)",
    "Accept": "application/json",
}

POST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ZethetaResync/1.0)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Environment / config helpers
# ---------------------------------------------------------------------------
def load_dotenv(path: str) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (no external deps)."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def get_app_code(args: argparse.Namespace) -> str:
    """Resolve app code from CLI arg, env var, or .env file."""
    if args.app_code:
        return args.app_code
    if args.env_file:
        load_dotenv(args.env_file)
    env_code = os.environ.get("APP_CODE", "").strip()
    if env_code:
        return env_code
    sys.exit("Error: --app-code is required (or set APP_CODE in environment / .env file).")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class ResultRecord:
    user_id: int
    course_id: int
    course_number: str
    course_name: str
    role_id: int
    report_uri: str
    percentage: Optional[float] = None
    status: str = "pending"
    message: str = ""
    api_response: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def fetch_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30, retries: int = 3) -> Dict[str, Any]:
    """Fetch and parse JSON from a URL. Returns empty dict on 404/403. Retries on 5xx / timeout."""
    req_headers = {**REQUEST_HEADERS, **(headers or {})}
    last_exc: Optional[Exception] = None

    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return {}
            if exc.code >= 500:
                last_exc = exc
                logging.warning(
                    "[RETRY] HTTP %s for %s (attempt %d/%d)",
                    exc.code, url, attempt + 1, retries + 1,
                )
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                break
            raise
        except urllib.error.URLError as exc:
            last_exc = exc
            logging.warning(
                "[RETRY] Network error for %s (attempt %d/%d): %s",
                url, attempt + 1, retries + 1, exc.reason,
            )
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            break
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON from {url}: {exc}") from exc

    if last_exc:
        raise RuntimeError(f"Max retries exceeded for {url}: {last_exc}") from last_exc
    return {}


def check_url_exists(url: str, timeout: int = 30, retries: int = 3) -> bool:
    """Send a HEAD request and return True only if the URL returns HTTP 200."""
    req_headers = dict(REQUEST_HEADERS)
    last_exc: Optional[Exception] = None

    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=req_headers, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status == 200
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return False
            if exc.code >= 500:
                last_exc = exc
                logging.warning(
                    "[RETRY] HEAD HTTP %s for %s (attempt %d/%d)",
                    exc.code, url, attempt + 1, retries + 1,
                )
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                break
            return False
        except urllib.error.URLError as exc:
            last_exc = exc
            logging.warning(
                "[RETRY] HEAD network error for %s (attempt %d/%d): %s",
                url, attempt + 1, retries + 1, exc.reason,
            )
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            break

    if last_exc:
        logging.warning("[RETRY] HEAD max retries exceeded for %s: %s", url, last_exc)
    return False


def post_json(url: str, payload: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    """POST JSON payload and return the parsed JSON response (or a success dict if body is not JSON)."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=POST_HEADERS, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return {"status": "OK (empty response)"}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "OK (non-JSON response)", "raw_response": raw[:500]}


# ---------------------------------------------------------------------------
# URI / normalization helpers
# ---------------------------------------------------------------------------
def normalize_course_name(name: str) -> str:
    """Replace spaces with underscores; keep existing underscores/hyphens intact."""
    return name.replace(" ", "_")


def build_report_uri(record: Dict[str, Any], is_tech: bool) -> str:
    """Build the S3 report URI for a submission record."""
    user_id = record["user_id"]
    course_number = record["course_number"]
    course_name = normalize_course_name(record["course_name"])

    if is_tech:
        return TECH_REPORT_BASE.format(
            user_id=user_id,
            course_number=course_number,
            course_name_underscore=course_name,
        )
    return NONTECH_REPORT_BASE.format(
        user_id=user_id,
        course_number=course_number,
        course_name_underscore=course_name,
    )


def build_report_uris(record: Dict[str, Any], is_tech: bool) -> List[str]:
    """
    Build report URIs to try, in order.
    For tech submissions, try the tech URL first, then the non-tech URL as a fallback
    (because some tech submissions were previously categorised as non-tech and their
    reports were generated under the non-tech path).
    """
    uris = [build_report_uri(record, is_tech)]
    if is_tech:
        uris.append(build_report_uri(record, False))
    return uris


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------
def fetch_submissions(submission_type: str) -> List[Dict[str, Any]]:
    """Fetch the submission list for the given type (large payload; long timeout)."""
    url = TECH_SUBMISSIONS_URL if submission_type == "tech" else NONTECH_SUBMISSIONS_URL
    logging.info("[FETCH] Retrieving %s submissions from %s", submission_type, url)
    data = fetch_json(url, timeout=120)
    if not isinstance(data, list):
        raise RuntimeError(f"Expected a JSON array from {url}, got {type(data).__name__}")
    logging.info("[FETCH] Received %d %s submission(s)", len(data), submission_type)
    return data


def extract_percentage(report_json: Dict[str, Any]) -> Optional[float]:
    """Extract a usable percentage from the report JSON."""
    # Primary field used in the update API payload.
    if "percentage" in report_json:
        return float(report_json["percentage"])
    # Sensible fallbacks if the report format differs.
    if "total_score" in report_json:
        return float(report_json["total_score"])
    if "score" in report_json:
        return float(report_json["score"])
    return None


def process_single_record(
    app_code: str,
    record: Dict[str, Any],
    is_tech: bool,
    dry_run: bool,
    job_id: str = None,
    run_type: str = None,
) -> ResultRecord:
    """
    For one submission record:
      1. Build the report URI.
      2. Fetch the report JSON.
      3. Extract the percentage.
      4. Call the update_submissions API.
    """
    from loki_config import set_context, clear_context
    set_context(job_id=job_id, run_type=run_type)
    try:
        result = _process_single_record_impl(app_code, record, is_tech, dry_run)
    finally:
        clear_context()
    return result


def _process_single_record_impl(
    app_code: str,
    record: Dict[str, Any],
    is_tech: bool,
    dry_run: bool,
) -> ResultRecord:
    """Internal implementation; context is managed by the wrapper above."""
    user_id = record.get("user_id")
    course_id = record.get("course_id")
    course_number = record.get("course_number", "")
    course_name = record.get("course_name", "")
    role_id = record.get("role_id")

    result = ResultRecord(
        user_id=user_id,
        course_id=course_id,
        course_number=course_number,
        course_name=course_name,
        role_id=role_id,
        report_uri=build_report_uri(record, is_tech),
    )

    if not all(isinstance(v, int) for v in (user_id, course_id, role_id)):
        result.status = "skipped"
        result.message = "Missing user_id, course_id, or role_id"
        return result

    # 1. Verify PDF exists in tech/non-tech S3 buckets and fetch the matching JSON for the score.
    report_json: Dict[str, Any] = {}
    tried_pdf_uris: List[str] = []
    used_pdf_uri = ""

    for json_uri in build_report_uris(record, is_tech):
        pdf_uri = json_uri.replace(".json", ".pdf")
        tried_pdf_uris.append(pdf_uri)
        if not check_url_exists(pdf_uri):
            continue
        used_pdf_uri = pdf_uri
        try:
            report_json = fetch_json(json_uri)
        except RuntimeError as exc:
            result.status = "error"
            result.message = f"Report fetch failed: {exc}"
            return result
        if report_json:
            break

    if not used_pdf_uri:
        result.status = "skipped"
        result.message = f"PDF report not found in any tried URI: {tried_pdf_uris}"
        return result

    if not report_json:
        result.status = "skipped"
        result.message = f"PDF exists but JSON report empty or inaccessible for: {used_pdf_uri}"
        return result

    result.report_uri = used_pdf_uri

    # 2. Extract percentage.
    percentage = extract_percentage(report_json)
    if percentage is None:
        result.status = "skipped"
        result.message = "No percentage/score found in report JSON"
        return result
    result.percentage = percentage

    if is_tech and used_pdf_uri.replace(".pdf", ".json") != build_report_uris(record, is_tech)[0]:
        logging.debug(
            "[FALLBACK] uid=%s used non-tech report URI: %s", user_id, used_pdf_uri
        )

    # 3. Build update payload with PDF report_uri.
    payload = {
        "app_code": app_code,
        "user_id": user_id,
        "course_id": course_id,
        "role_id": role_id,
        "report_pdf": "",
        "report_uri": result.report_uri,
        "percentage": percentage,
    }

    if dry_run:
        result.status = "dry_run"
        result.message = "Would POST payload (dry run)"
        result.api_response = payload
        redacted = {**payload, "app_code": f"{app_code[:4]}...{app_code[-4:]}" if len(app_code) > 12 else "****"}
        logging.debug("[PAYLOAD] uid=%s payload=%s", user_id, json.dumps(redacted, default=str))
        return result

    # 4. Call update API.
    try:
        response = post_json(UPDATE_API_URL, payload)
        result.status = "success"
        result.message = str(response.get("status", "OK"))
        result.api_response = response
        logging.info(
            "[API] uid=%s response=%s",
            user_id,
            json.dumps(response, default=str),
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        result.status = "error"
        result.message = f"API returned HTTP {exc.code}: {body}"
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.message = f"API call failed: {exc}"

    return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def write_csv(results: List[ResultRecord], path: str) -> None:
    """Write results to a CSV file."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "user_id", "course_id", "course_number", "course_name",
            "role_id", "report_uri", "percentage", "status", "message", "api_response",
        ])
        for r in results:
            writer.writerow([
                r.user_id, r.course_id, r.course_number, r.course_name,
                r.role_id, r.report_uri, r.percentage, r.status, r.message,
                json.dumps(r.api_response, default=str),
            ])
    logging.info("[CSV] Wrote %d row(s) to %s", len(results), path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resync Zetheta submission scores from report JSONs to the update_submissions API.",
    )
    parser.add_argument(
        "--app-code",
        default="",
        help=(
            "App code / API key required by the update_submissions endpoint. "
            "Can also be supplied via APP_CODE env var or .env file."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=".env" if os.path.isfile(".env") else "",
        help="Path to a .env file containing APP_CODE (default: .env if present).",
    )
    parser.add_argument(
        "--type",
        choices=["tech", "nontech", "all"],
        default="all",
        help="Which submission type to process (default: all).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N records per type (0 = unlimited).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Concurrent worker threads for report/API calls (default: 5).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build payloads and fetch reports but do not POST to the update API.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        dest="test_mode",
        help="Validate connectivity and report URLs on a small sample; no updates.",
    )
    parser.add_argument(
        "--output",
        help="Optional CSV path to write a summary of all processed records.",
    )
    parser.add_argument(
        "--user-ids",
        default="",
        help="Comma-separated list of user IDs to process (default: all).",
    )
    return parser


def run_test_mode(args: argparse.Namespace) -> int:
    """Quick validation: fetch both submission lists and a few sample reports."""
    logging.info("[TEST] Validating endpoints and sample report URIs...")
    ok = 0
    fail = 0

    for stype in ("tech", "nontech"):
        try:
            submissions = fetch_submissions(stype)
        except RuntimeError as exc:
            logging.error("[TEST] %s submissions fetch failed: %s", stype, exc)
            fail += 1
            continue

        if not submissions:
            logging.warning("[TEST] %s submissions list is empty", stype)
            fail += 1
            continue

        logging.info("[TEST] %s submissions fetch OK (%d records)", stype, len(submissions))

        # Probe first 3 records (with fallback URI for tech)
        is_tech = stype == "tech"
        for record in submissions[:3]:
            for uri in build_report_uris(record, is_tech):
                try:
                    report = fetch_json(uri)
                    if report:
                        pct = extract_percentage(report)
                        logging.info(
                            "[TEST] %s report OK  uid=%s pct=%s uri=%s",
                            stype, record.get("user_id"), pct, uri,
                        )
                        ok += 1
                        break
                except RuntimeError as exc:
                    logging.error(
                        "[TEST] %s report error uid=%s uri=%s: %s",
                        stype, record.get("user_id"), uri, exc,
                    )
            else:
                logging.warning(
                    "[TEST] %s report empty/inaccessible uid=%s (tried all URIs)",
                    stype, record.get("user_id"),
                )
                fail += 1

    logging.info("[TEST] Validation complete: ok=%d, fail=%d", ok, fail)
    return 0 if fail == 0 else 1


def run_resync(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Programmatic entry point to run the resync job.
    Returns a dict with job metadata, counts, and per-record results.
    """
    from loki_config import set_context, clear_context
    job_id = config.get("job_id", str(int(time.time())))
    run_type = config.get("type", "all")
    set_context(job_id=job_id, run_type=run_type)

    try:
        result = _run_resync_impl(config, job_id, run_type)
    finally:
        clear_context()

    return result


def _run_resync_impl(config: Dict[str, Any], job_id: str, run_type: str) -> Dict[str, Any]:
    """Internal implementation; context is managed by the wrapper above."""
    limit = int(config.get("limit", 0))
    workers = int(config.get("workers", 5))
    dry_run = bool(config.get("dry_run", False))
    output = config.get("output", "")
    verbose = bool(config.get("verbose", False))
    app_code = config.get("app_code", "")
    user_ids = config.get("user_ids", "")
    allowed_user_ids = set()
    if user_ids:
        allowed_user_ids = {uid.strip() for uid in str(user_ids).split(",") if uid.strip()}

    if not app_code:
        raise ValueError("app_code is required")

    masked = f"{app_code[:4]}...{app_code[-4:]}" if len(app_code) > 12 else "****"
    logging.info("[CONFIG] Using app code: %s", masked)

    types_to_process = []
    if run_type in ("tech", "all"):
        types_to_process.append("tech")
    if run_type in ("nontech", "all"):
        types_to_process.append("nontech")

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    all_results: List[ResultRecord] = []

    for submission_type in types_to_process:
        try:
            submissions = fetch_submissions(submission_type)
        except RuntimeError as exc:
            logging.error("[ERROR] Could not fetch %s submissions: %s", submission_type, exc)
            raise

        if allowed_user_ids:
            submissions = [
                s for s in submissions if str(s.get("user_id")) in allowed_user_ids
            ]
            logging.info(
                "[FILTER] Processing %s %s submission(s) matching user IDs: %s",
                len(submissions),
                submission_type,
                ",".join(sorted(allowed_user_ids)),
            )

        if limit > 0:
            submissions = submissions[:limit]

        is_tech = submission_type == "tech"
        total = len(submissions)
        logging.info(
            "[PROCESS] Starting %s records (%s, workers=%d, dry_run=%s)",
            total,
            submission_type,
            workers,
            dry_run,
        )

        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_record = {
                executor.submit(
                    process_single_record, app_code, record, is_tech, dry_run, job_id, run_type
                ): record
                for record in submissions
            }
            for future in as_completed(future_to_record):
                record = future_to_record[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    logging.error(
                        "[ERROR] Unhandled exception for user_id=%s: %s",
                        record.get("user_id"),
                        exc,
                    )
                    result = ResultRecord(
                        user_id=record.get("user_id"),
                        course_id=record.get("course_id"),
                        course_number=record.get("course_number", ""),
                        course_name=record.get("course_name", ""),
                        role_id=record.get("role_id"),
                        report_uri=build_report_uri(record, is_tech),
                        status="error",
                        message=f"Unhandled exception: {exc}",
                    )

                all_results.append(result)
                completed += 1

                logging.info(
                    "[RECORD] status=%s user_id=%s course_id=%s percentage=%s message=%s",
                    result.status,
                    result.user_id,
                    result.course_id,
                    result.percentage,
                    result.message,
                )

                if completed % 50 == 0 or completed == total:
                    logging.info(
                        "[PROGRESS] %s: %d/%d processed", submission_type, completed, total
                    )

        # Per-type summary.
        statuses = {}
        for r in all_results[-total:]:
            statuses[r.status] = statuses.get(r.status, 0) + 1
        logging.info(
            "[SUMMARY] %s complete: %s",
            submission_type,
            ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())),
        )

    finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Aggregate counts.
    counts = {
        "total": len(all_results),
        "success": 0,
        "error": 0,
        "skipped": 0,
        "dry_run": 0,
    }
    for r in all_results:
        counts[r.status] = counts.get(r.status, 0) + 1

    global_statuses = {}
    for r in all_results:
        global_statuses[r.status] = global_statuses.get(r.status, 0) + 1

    logging.info("[DONE] Total records processed: %d", len(all_results))
    logging.info(
        "[DONE] Status breakdown: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(global_statuses.items())),
    )

    if output:
        write_csv(all_results, output)

    return {
        "job_id": job_id,
        "type": run_type,
        "started_at": started_at,
        "finished_at": finished_at,
        **counts,
        "output_csv": output if output else None,
        "results": [result_to_dict(r) for r in all_results],
    }


def result_to_dict(r: ResultRecord) -> Dict[str, Any]:
    """Convert a ResultRecord to a plain dict."""
    return {
        "user_id": r.user_id,
        "course_id": r.course_id,
        "course_number": r.course_number,
        "course_name": r.course_name,
        "role_id": r.role_id,
        "report_uri": r.report_uri,
        "percentage": r.percentage,
        "status": r.status,
        "message": r.message,
        "api_response": r.api_response,
    }


def main() -> int:
    args = build_parser().parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.test_mode:
        return run_test_mode(args)

    app_code = get_app_code(args)
    config = {
        "app_code": app_code,
        "type": args.type,
        "limit": args.limit,
        "workers": args.workers,
        "dry_run": args.dry_run,
        "output": args.output,
        "verbose": args.verbose,
        "user_ids": args.user_ids,
    }

    try:
        run_resync(config)
    except Exception as exc:  # noqa: BLE001
        logging.error("[ERROR] Resync failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
