"""
Loki logging configuration and handler.

Pushes JSON logs to a Grafana Loki instance with labels for job, job_id,
run_type, level, and user_id. Gracefully degrades if Loki is unreachable.
"""

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

DEFAULT_LOKI_URL = "http://localhost:3100"
PUSH_PATH = "/loki/api/v1/push"


class LokiHandler(logging.Handler):
    """
    A logging.Handler that pushes log records to Loki.

    Logs are buffered in a thread-safe queue and flushed to Loki by a background
    thread. The handler includes per-record labels where available.
    """

    def __init__(
        self,
        loki_url: str = DEFAULT_LOKI_URL,
        labels: Dict[str, str] = None,
        timeout: int = 10,
        flush_interval: float = 2.0,
        max_batch: int = 100,
    ) -> None:
        super().__init__()
        self.loki_url = loki_url.rstrip("/")
        self.labels = labels or {}
        self.timeout = timeout
        self.flush_interval = flush_interval
        self.max_batch = max_batch
        self._queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self._flush_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self.setFormatter(LokiJsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "ts": int(time.time() * 1e9),  # nanoseconds
                "line": self.format(record),
                "labels": self._record_labels(record),
            }
            self._queue.put(payload, block=False)
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def _record_labels(self, record: logging.LogRecord) -> Dict[str, str]:
        labels = dict(self.labels)
        labels["level"] = record.levelname
        # If the log record has extra attributes from the resync job, use them.
        for key in ("job_id", "run_type", "user_id"):
            value = getattr(record, key, None)
            if value is not None:
                labels[key] = str(value)
        return labels

    def flush(self) -> None:
        self._flush_event.set()
        self._worker.join(timeout=self.timeout + 5)

    def close(self) -> None:
        self._flush_event.set()
        self._worker.join(timeout=self.timeout + 5)
        super().close()

    def _worker_loop(self) -> None:
        while not self._flush_event.is_set() or not self._queue.empty():
            batch: List[Dict[str, Any]] = []
            deadline = time.time() + self.flush_interval
            while len(batch) < self.max_batch and time.time() < deadline:
                try:
                    batch.append(self._queue.get(timeout=0.1))
                except queue.Empty:
                    if self._flush_event.is_set():
                        break
                    continue
            if batch:
                self._push(batch)

    def _push(self, batch: List[Dict[str, Any]]) -> None:
        streams: Dict[str, Dict[str, Any]] = {}
        for item in batch:
            label_key = json.dumps(item["labels"], sort_keys=True)
            if label_key not in streams:
                streams[label_key] = {
                    "stream": item["labels"],
                    "values": [],
                }
            streams[label_key]["values"].append([str(item["ts"]), item["line"]])

        payload = {"streams": list(streams.values())}
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.loki_url}{PUSH_PATH}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "zetheta-resync/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            # Log to stderr but do not crash the main application if Loki is down.
            print(f"[LOKI] HTTP {exc.code}: failed to push {len(batch)} log(s)", file=os.sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[LOKI] Failed to push {len(batch)} log(s): {exc}", file=os.sys.stderr)


class LokiJsonFormatter(logging.Formatter):
    """Format log records as compact JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "message": record.getMessage(),
            "level": record.levelname,
            "logger": record.name,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
        }
        if record.exc_info:
            obj["exception"] = self.formatException(record.exc_info)
        # Merge in extra attributes from the log record.
        for key, value in record.__dict__.items():
            if key not in obj and key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "asctime", "message",
            ):
                obj[key] = value
        return json.dumps(obj, default=str)


class ContextFilter(logging.Filter):
    """Inject thread-local context attributes into every LogRecord."""

    _local = threading.local()

    @classmethod
    def set_context(cls, job_id: str = None, run_type: str = None) -> None:
        cls._local.job_id = job_id
        cls._local.run_type = run_type

    @classmethod
    def clear_context(cls) -> None:
        cls._local.job_id = None
        cls._local.run_type = None

    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id = getattr(self._local, "job_id", None)
        record.run_type = getattr(self._local, "run_type", None)
        return True


def setup_logging(
    level: int = logging.INFO,
    loki_url: str = None,
    labels: Dict[str, str] = None,
) -> logging.Logger:
    """
    Configure root logging to stdout and optionally to Loki.

    Returns the root logger.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    context_filter = ContextFilter()
    logging.getLogger().addFilter(context_filter)

    if loki_url:
        handler = LokiHandler(loki_url=loki_url, labels=labels)
        handler.setLevel(level)
        logging.getLogger().addHandler(handler)

    return logging.getLogger()


def log_extra(job_id: str = None, run_type: str = None, user_id: int = None) -> Dict[str, Any]:
    """Helper to build logging extra kwargs for Loki labels."""
    extra = {}
    if job_id:
        extra["job_id"] = job_id
    if run_type:
        extra["run_type"] = run_type
    if user_id is not None:
        extra["user_id"] = str(user_id)
    return {"extra": extra}


def set_context(job_id: str = None, run_type: str = None) -> None:
    """Set thread-local context attributes for Loki labels."""
    ContextFilter.set_context(job_id, run_type)


def clear_context() -> None:
    """Clear thread-local context attributes."""
    ContextFilter.clear_context()
