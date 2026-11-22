"""The service loop: read, decide, lock (only lock), confirm, log, notify."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import random
import signal
import time
import tomllib
from dataclasses import fields

import requests

from .core import Config, Reading, decide, iso, parse_dt, poll_delay_min, roll_day, where
from .util import Paths, RedactFilter, add_secret, append_jsonl, redact, utcnow, write_atomic

log = logging.getLogger("ev9")

EXIT_CONFIG = 78  # systemd unit: RestartPreventExitStatus=78 (bad password / bad config: never loop)


def setup_logging(level: str) -> None:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    lvl = getattr(logging, (level or "INFO").upper(), logging.INFO)
    root.setLevel(lvl)
    logging.getLogger("hyundai_kia_connect_api").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_config(paths: Paths) -> Config:
    data = {}
    if paths.config.exists():
        data = tomllib.loads(paths.config.read_text())
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise SystemExit(f"unknown config keys: {sorted(unknown)}")
    return Config(**data)


def load_secrets(paths: Paths) -> dict:
    """Environment first (systemd EnvironmentFile), else the secrets file when readable (root CLI)."""
    vals = {k: os.environ.get(k, "") for k in ("KIA_USERNAME", "KIA_PASSWORD", "NOTIFY_WEBHOOK_URL")}
    if (not vals["KIA_USERNAME"] or not vals["KIA_PASSWORD"]) and paths.secrets.exists() and os.access(paths.secrets, os.R_OK):
        for line in paths.secrets.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in vals and not vals[k.strip()]:
                    vals[k.strip()] = v.strip()
    add_secret(vals["KIA_USERNAME"], vals["KIA_PASSWORD"], vals["NOTIFY_WEBHOOK_URL"])
    return vals


def load_zones(paths: Paths) -> list:
    """No-lock zones from zones.json (managed by `ev9-autolock zone ...`). A broken file raises:
    better to stop than to lock inside a zone the owner thinks is set."""
    if not paths.zones.exists():
        return []
    data = json.loads(paths.zones.read_text())
    zones = data.get("zones", []) if isinstance(data, dict) else []
    for z in zones:
        if not (isinstance(z.get("name"), str) and -90 <= float(z["lat"]) <= 90 and -180 <= float(z["lon"]) <= 180
                and 10 <= float(z.get("radius_m", 150)) <= 5000):
            raise ValueError(f"bad zone in {paths.zones}: {z.get('name')!r}")
    return zones


class State:
    def __init__(self, paths: Paths):
        self.paths = paths
        try:
            self.d = json.loads(paths.state.read_text())
        except FileNotFoundError:
            self.d = {"version": 1, "auth_state": "ok", "notified": {}}

    def save(self) -> None:
        write_atomic(self.paths.state, json.dumps(self.d, indent=1, default=str))


class Service:
    def __init__(self, paths: Paths, cfg: Config, secrets: dict, client_factory, sleep=time.sleep, now=utcnow):
        self.paths, self.cfg, self.secrets = paths, cfg, secrets
        self.client_factory, self.sleep, self.now = client_factory, sleep, now
        self.state = State(paths)
        self.client = None
        self.stop = False
        self.last: Reading | None = None
        self.fails = 0

    # -- helpers ------------------------------------------------------------
    @property
    def st(self) -> dict:
        return self.state.d

    def event(self, name: str, detail: str = "", notify: bool = False, **extra) -> None:
        rec = {"t": iso(self.now()), "event": name, "detail": detail, **extra}
        append_jsonl(self.paths.events, rec)
        log.info("event=%s %s", name, detail)
        if notify:
            self.notify(name, detail)

    def notify_once(self, key: str, name: str, detail: str) -> None:
        n = self.st.setdefault("notified", {})
        if not n.get(key):
            n[key] = True
            self.event(name, detail, notify=True)

    def notify(self, name: str, detail: str) -> None:
        url = self.secrets.get("NOTIFY_WEBHOOK_URL")
        if not url:
            return
        if self.cfg.quiet_hours_mode == "lock_only" and self.cfg.quiet_hours and name == "locked":
            from .core import in_quiet_hours
            if in_quiet_hours(self.cfg, self.now()):
                return
        body = {"event": name, "time": iso(self.now()), "vehicle": "EV9", "detail": detail}
        try:
            if "ntfy" in url:
                requests.post(url, data=f"EV9 {name}: {detail}".encode(), headers={"Title": f"EV9 {name}"}, timeout=10)
            else:
                requests.post(url, json=body, timeout=10)
        except Exception as e:  # never blocks or retries a lock
            log.warning("notify failed: %s", redact(type(e).__name__))

    def wait(self, seconds: float) -> None:
        remaining = float(seconds)
        while not self.stop and remaining > 0:
            step = min(1.0, remaining)
            self.sleep(step)
            remaining -= step

    def get_client(self):
        if self.client is None:
            self.client = self.client_factory(self.st, self.state.save)
            self.client.ensure()
        return self.client

    # -- one pass -------------------------------------------------------------
    def poll_once(self) -> float:
        """One pass. Returns minutes to wait before the next."""
        from .kia import BadPassword, OTPRequired

        now = self.now()
        roll_day(self.st, self.cfg, now)
        if self.st.get("auth_state") == "needs_otp":
            if self.paths.token.exists() and self.paths.token.stat().st_mtime > self.st.get("needs_otp_since_ts", 0):
                self.st["auth_state"] = "ok"
                self.st.setdefault("notified", {})["needs_otp"] = False
                self.client = None
                self.event("signin_restored", "new sign-in found")
            else:
                return 10
        if self.st.get("logins_today", 0) >= 4:
            self.notify_once("login_cap", "login_cap", "4 sign-ins today; pausing until tomorrow")
            return 30
        try:
            client = self.get_client()
            r = client.read()
            self.fails = 0
            self.st["last_ok_at"] = iso(r.t)
        except OTPRequired as e:
            self.client = None
            self.st.update(auth_state="needs_otp", needs_otp_since=iso(now), needs_otp_since_ts=time.time())
            self.notify_once("needs_otp", "needs_otp", "Kia needs a new one-time code: run `ev9-autolock setup-otp`")
            log.warning("sign-in needed: %s", redact(e))
            return 10
        except BadPassword as e:
            self.event("auth_failed", f"Kia refused the password ({redact(e)}); stopping, no retries", notify=True)
            self.state.save()
            raise SystemExit(EXIT_CONFIG)
        except SystemExit:
            raise
        except Exception as e:
            self.fails += 1
            self.client = None if self.fails >= 3 else self.client
            name = type(e).__name__
            if name in ("RateLimitingError", "DuplicateRequestError"):
                delay = 60
            elif name in ("ServiceTemporaryUnavailable", "ConnectionError", "Timeout", "ReadTimeout",
                          "ConnectTimeout", "HTTPError", "RequestException", "APIError"):
                delay = min(30, 2 ** (self.fails - 1)) * random.uniform(0.8, 1.2)
            else:
                delay = 15
            append_jsonl(self.paths.events, {"t": iso(now), "event": "error", "error": f"{name}: {redact(e)}"})
            log.warning("read failed (%s: %s); retry in %.0f min", name, redact(e), delay)
            return delay

        paused = self.paths.pause.exists()
        try:
            zones = load_zones(self.paths)
        except Exception as e:
            self.notify_once("zones_bad", "zones_bad", f"cannot read no-lock zones ({redact(e)}); not locking")
            log.warning("zones unreadable: %s", redact(e))
            self.last = r
            self.state.save()
            return poll_delay_min(r, self.st, self.cfg, self.now())
        self.st.setdefault("notified", {})["zones_bad"] = False
        action, detail = decide(r, self.st, self.cfg, r.t, paused, zones)
        unl = parse_dt(self.st.get("unlocked_since"))
        unl_min = f"{(r.t - unl).total_seconds() / 60:.0f}m" if unl else "-"
        zone = "-"
        if zones:
            kind, name = where(r, self.st, zones)
            zone = name if kind == "zone" else kind
        log.info("poll locked=%s on=%s acc=%s park=%s open=%s reported=%s unlocked_for=%s zone=%s action=%s %s",
                 r.locked, r.engine, r.ign3, r.park, sum(1 for d in [*r.doors, r.trunk, r.hood] if d),
                 iso(r.car_sync), unl_min, zone, action, detail)
        n = self.st.setdefault("notified", {})
        if action == "blocked":
            self.notify_once("door_open", "door_open_long", detail)
        elif action == "cap":
            self.notify_once("cap_reached", "cap_reached", detail)
        elif action == "lock":
            self.lock_and_confirm(detail)
        if action != "blocked":
            n["door_open"] = False if action in ("locked", "in_use") else n.get("door_open", False)
        self.last = r
        self.state.save()
        return poll_delay_min(r, self.st, self.cfg, self.now())

    # -- locking ----------------------------------------------------------------
    def lock_and_confirm(self, reason: str) -> None:
        now = self.now()
        if self.cfg.dry_run:
            self.st["next_lock_allowed_at"] = iso(now + dt.timedelta(minutes=30))
            self.event("would_lock", f"dry run: {reason}", notify=True)
            return
        client = self.client
        self.st["locks_today"] = self.st.get("locks_today", 0) + 1
        self.st["last_lock_at"] = iso(now)
        self.st["next_lock_allowed_at"] = iso(now + dt.timedelta(minutes=self.cfg.min_gap_between_locks_min))
        self.state.save()  # counted before sending: a crash can't cause an uncounted second lock
        self.event("lock_sent", reason)
        t0 = time.monotonic()
        try:
            xid = client.lock()
            status = client.action_status(xid, self.cfg.lock_confirm_timeout_s)
        except Exception as e:
            status = f"error {type(e).__name__}: {redact(e)}"
        secs = time.monotonic() - t0
        confirmed = False
        for wait_s in (3, 60):
            self.wait(wait_s)
            try:
                r = client.read()
                if r.locked is True:
                    confirmed = True
                    break
            except Exception as e:
                log.warning("confirm read failed: %s", redact(e))
        if not confirmed:
            last_force = parse_dt(self.st.get("last_force_refresh_at"))
            if not last_force or (self.now() - last_force).total_seconds() / 60 >= self.cfg.force_refresh_min_interval_min:
                try:
                    self.st["last_force_refresh_at"] = iso(self.now())
                    client.force_refresh()
                    self.wait(5)
                    confirmed = client.read().locked is True
                except Exception as e:
                    log.warning("forced refresh failed: %s", redact(e))
        if confirmed:
            self.st["unlocked_since"] = None
            self.st["attempts_unconfirmed"] = 0
            self.event("locked", f"{reason}; car answered {status} in {secs:.0f}s; status shows locked", notify=True)
        else:
            a = self.st.get("attempts_unconfirmed", 0) + 1
            self.st["attempts_unconfirmed"] = a
            back = {1: 10, 2: 30}.get(a)
            if back:
                self.st["next_lock_allowed_at"] = iso(self.now() + dt.timedelta(minutes=back))
            else:
                self.st["locks_today"] = max(self.st.get("locks_today", 0), self.cfg.max_lock_attempts_per_day)
            self.event("lock_unconfirmed", f"car answered {status}; status still not locked (attempt {a})", notify=True)
        self.state.save()

    # -- loop ---------------------------------------------------------------------
    def run(self, once: bool = False) -> None:
        lockf = open(self.paths.lock, "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another ev9-autolock is running")

        def _term(*_):
            self.stop = True
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        log.info("ev9-autolock started dry_run=%s threshold=%sm cap=%s/day", self.cfg.dry_run,
                 self.cfg.unlocked_threshold_min, self.cfg.max_lock_attempts_per_day)
        try:
            while not self.stop:
                delay = self.poll_once()
                self.state.save()
                if once:
                    break
                self.wait(delay * 60)
        finally:
            self.state.save()
            log.info("stopped")
            if not once:
                self.event("service_stopped", "", notify=False)
