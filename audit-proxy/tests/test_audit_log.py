import json
import os
import threading
from datetime import datetime, timedelta

from app.audit_log import AuditLogger


class Clock:
    def __init__(self, logger: AuditLogger, start: datetime):
        self.t = start
        logger.now = lambda: self.t  # type: ignore[method-assign]


def test_concurrent_writes_never_interleave(tmp_path):
    lg = AuditLogger(str(tmp_path))
    big = "x" * 20000

    def worker(n):
        for i in range(50):
            lg.write({"worker": n, "i": i, "pad": big})

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 400
    assert all(json.loads(line)["pad"] == big for line in lines)


def test_daily_rotation_and_retention(tmp_path):
    lg = AuditLogger(str(tmp_path), retention_days=3)
    clock = Clock(lg, lg.now().replace(year=2026, month=11, day=1, hour=23))
    old = tmp_path / "audit-2026-10-01.jsonl"
    old.write_text("{}\n")
    lg.write({"n": 1})
    clock.t += timedelta(hours=2)  # next day (Jakarta time)
    lg.write({"n": 2})
    assert (tmp_path / "audit-2026-11-01.jsonl").read_text().strip() == '{"n":1}'
    assert (tmp_path / "audit.jsonl").read_text().strip() == '{"n":2}'
    assert not old.exists()  # older than retention -> pruned
    clock.t += timedelta(days=1)
    lg.write({"n": 3})
    assert (tmp_path / "audit-2026-11-02.jsonl").exists()


def test_rotation_on_restart_uses_file_mtime(tmp_path):
    f = tmp_path / "audit.jsonl"
    f.write_text('{"n":0}\n')
    yesterday = datetime(2026, 11, 12, 10).timestamp()
    os.utime(f, (yesterday, yesterday))
    lg = AuditLogger(str(tmp_path))
    Clock(lg, lg.now().replace(year=2026, month=11, day=13, hour=9))
    lg.write({"n": 1})
    assert (tmp_path / "audit-2026-11-12.jsonl").read_text() == '{"n":0}\n'
    assert f.read_text() == '{"n":1}\n'


def test_failure_then_recovery(tmp_path):
    target = tmp_path / "logs"
    target.write_text("i am a file")
    lg = AuditLogger(str(target))
    assert lg.write({"n": 1}) is False
    assert lg.status()["log_ok"] is False and lg.errors == 1
    target.unlink()
    assert lg.write({"n": 2}) is True
    assert lg.status()["log_ok"] is True
