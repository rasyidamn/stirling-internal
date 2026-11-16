"""Append-only JSONL audit writer with daily rotation, retention and optional
remote syslog copy. Failures never propagate to the caller; they are counted
and exposed through status() for /healthz."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class AuditLogger:
    def __init__(self, log_dir: str, filename: str = "audit.jsonl", retention_days: int = 365,
                 tz: str = "Asia/Jakarta", syslog_host: str = "", syslog_port: int = 514,
                 syslog_protocol: str = "udp") -> None:
        self.dir = Path(log_dir)
        self.filename = filename
        self.path = self.dir / filename
        self.retention_days = retention_days
        self.tz = ZoneInfo(tz)
        self._lock = threading.Lock()
        self._fd: int | None = None
        self._file_date: date | None = None
        self.ok = True
        self.errors = 0
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        self.written = 0
        self._syslog: logging.Logger | None = None
        if syslog_host:
            self._setup_syslog(syslog_host, syslog_port, syslog_protocol)

    # ---- public -------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def write(self, record: dict) -> bool:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        ok = self._write_file(line)
        if self._syslog is not None:
            try:
                self._syslog.info(line)
            except Exception as exc:  # noqa: BLE001
                print(f"audit-proxy: syslog send failed: {exc}", file=sys.stderr)
        return ok

    def status(self) -> dict:
        return {
            "log_ok": self.ok,
            "log_path": str(self.path),
            "log_errors": self.errors,
            "last_log_error": self.last_error,
            "last_log_error_at": self.last_error_at,
            "events_written": self.written,
            "syslog": self._syslog is not None,
        }

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    # ---- internals ----------------------------------------------------
    def _write_file(self, line: str) -> bool:
        data = (line + "\n").encode("utf-8")
        with self._lock:
            try:
                self._rotate_if_needed()
                if self._fd is None:
                    self._open()
                # One write() per event on an O_APPEND fd: lines never interleave.
                n = os.write(self._fd, data)
                if n != len(data):
                    raise OSError(f"short write {n}/{len(data)}")
                self.ok = True
                self.written += 1
                return True
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fd = None
                return False

    def _open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        if self._file_date is None:
            st = os.fstat(self._fd)
            if st.st_size > 0:
                self._file_date = datetime.fromtimestamp(st.st_mtime, self.tz).date()
            else:
                self._file_date = self.now().date()

    def _rotate_if_needed(self) -> None:
        today = self.now().date()
        if self._file_date is None and self.path.exists() and self.path.stat().st_size > 0:
            self._file_date = datetime.fromtimestamp(self.path.stat().st_mtime, self.tz).date()
        if self._file_date is None or self._file_date == today:
            return
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self.path.exists():
            stem, suffix = os.path.splitext(self.filename)
            target = self.dir / f"{stem}-{self._file_date.isoformat()}{suffix}"
            i = 1
            while target.exists():
                target = self.dir / f"{stem}-{self._file_date.isoformat()}.{i}{suffix}"
                i += 1
            os.rename(self.path, target)
        self._file_date = today
        self._prune(today)

    def _prune(self, today: date) -> None:
        if self.retention_days <= 0:
            return
        cutoff = today - timedelta(days=self.retention_days)
        stem, suffix = os.path.splitext(self.filename)
        for f in self.dir.glob(f"{stem}-*{suffix}"):
            try:
                d = date.fromisoformat(f.name[len(stem) + 1:len(stem) + 11])
            except ValueError:
                continue
            if d < cutoff:
                try:
                    f.unlink()
                except OSError as exc:
                    print(f"audit-proxy: cannot delete old log {f}: {exc}", file=sys.stderr)

    def _fail(self, exc: Exception) -> None:
        self.ok = False
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.last_error_at = self.now().isoformat()
        print(f"audit-proxy: AUDIT LOG WRITE FAILED: {self.last_error}", file=sys.stderr, flush=True)

    def _setup_syslog(self, host: str, port: int, protocol: str) -> None:
        socktype = socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
        try:
            handler = logging.handlers.SysLogHandler(
                address=(host, port), facility=logging.handlers.SysLogHandler.LOG_LOCAL0,
                socktype=socktype)
        except OSError as exc:
            print(f"audit-proxy: syslog {host}:{port}/{protocol} unavailable: {exc}", file=sys.stderr)
            return
        if protocol == "tcp":
            # TCP syslog needs a frame delimiter: LF framing (rsyslog/syslog-ng default).
            handler.append_nul = False
            handler.setFormatter(logging.Formatter("stirling-audit: %(message)s\n"))
        else:
            handler.setFormatter(logging.Formatter("stirling-audit: %(message)s"))
        lg = logging.getLogger("audit-proxy.syslog")
        lg.propagate = False
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)
        self._syslog = lg
