from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SECRETS: set[str] = set()


def add_secret(*vals) -> None:
    for v in vals:
        if v and len(str(v)) >= 4:
            _SECRETS.add(str(v))


def redact(s) -> str:
    s = str(s)
    for v in sorted(_SECRETS, key=len, reverse=True):
        s = s.replace(v, "***")
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "***@***", s)
    s = re.sub(r"(?i)\b(sid|rmtoken|otpkey|otp|token|access_token|refresh_token|password|deviceid|vinkey|xid)(\W{1,4})[^\s\"',}]+",
               r"\1\2***", s)
    s = re.sub(r"\b[A-HJ-NPR-Z0-9]{17}\b", "VIN***", s)
    return s[:500]


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg, record.args = redact(msg), None
        return True


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Paths:
    state_dir: Path
    config: Path
    secrets: Path

    @property
    def token(self) -> Path:
        return self.state_dir / "token.json"

    @property
    def state(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def readings(self) -> Path:
        return self.state_dir / "readings.jsonl"

    @property
    def events(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def pause(self) -> Path:
        return self.state_dir / "PAUSE"

    @property
    def lock(self) -> Path:
        return self.state_dir / "run.lock"


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def append_jsonl(path: Path, obj: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(obj, separators=(",", ":"), default=str) + "\n")
