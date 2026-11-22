"""Pure decision logic: no network, no files. Easy to test."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


@dataclass
class Config:
    unlocked_threshold_min: float = 5
    poll_active_min: float = 2
    poll_idle_min: float = 10
    active_window_min: float = 10
    max_lock_attempts_per_day: int = 3
    min_gap_between_locks_min: float = 10
    lock_confirm_timeout_s: int = 150
    force_refresh_min_interval_min: float = 120
    quiet_hours: str = ""
    quiet_hours_mode: str = "skip"  # skip | lock_only
    timezone: str = "America/Chicago"
    dry_run: bool = True
    log_level: str = "INFO"
    region: int = 3
    brand: int = 1
    vehicle_vin_suffix: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass
class Reading:
    """One cached status read, already parsed."""
    t: dt.datetime                      # when we read it (UTC)
    car_sync: dt.datetime | None        # when the car last reported (UTC)
    locked: bool | None
    engine: bool | None                 # powered on
    ign3: bool | None                   # accessory power
    park: bool | None                   # transCond: True = in Park
    doors: list = field(default_factory=lambda: [None] * 4)
    trunk: object = None
    hood: object = None
    charging: bool | None = None
    plugged: object = None
    soc: object = None

    def openings_known(self) -> bool:
        return all(v is not None for v in [*self.doors, self.trunk, self.hood])

    def any_open(self) -> bool:
        return any(bool(v) for v in [*self.doors, self.trunk, self.hood])

    def in_use(self) -> bool:
        return bool(self.engine) or bool(self.ign3) or self.park is False


def parse_dt(s):
    if not s:
        return None
    if isinstance(s, dt.datetime):
        return s if s.tzinfo else s.replace(tzinfo=dt.UTC)
    return dt.datetime.fromisoformat(s)


def iso(t):
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone(dt.UTC).isoformat(timespec="seconds")


def in_quiet_hours(cfg: Config, now: dt.datetime) -> bool:
    if not cfg.quiet_hours:
        return False
    try:
        a, b = cfg.quiet_hours.split("-")
        ha, ma = map(int, a.split(":"))
        hb, mb = map(int, b.split(":"))
    except ValueError:
        return False
    local = now.astimezone(cfg.tz)
    m = local.hour * 60 + local.minute
    start, end = ha * 60 + ma, hb * 60 + mb
    return (start <= m < end) if start <= end else (m >= start or m < end)


def roll_day(st: dict, cfg: Config, now: dt.datetime) -> None:
    """Reset per-day counters when the local date changes."""
    day = now.astimezone(cfg.tz).date().isoformat()
    if st.get("day") != day:
        st["day"] = day
        st["locks_today"] = 0
        st["logins_today"] = 0
        st["attempts_unconfirmed"] = 0
        st.setdefault("notified", {})["cap_reached"] = False


def decide(r: Reading, st: dict, cfg: Config, now: dt.datetime, paused: bool) -> tuple[str, str]:
    """Return (action, detail). Mutates st['unlocked_since'] and 'notified' flags.

    actions: paused, quiet, unknown, locked, in_use, wait, blocked, cap, backoff, lock
    """
    notified = st.setdefault("notified", {})
    if paused:
        return "paused", "pause flag set"
    if in_quiet_hours(cfg, now) and cfg.quiet_hours_mode == "skip":
        return "quiet", cfg.quiet_hours
    if r.locked is None or r.engine is None or r.ign3 is None or r.park is None:
        return "unknown", "lock or power/park state unknown"
    if r.locked is True:
        st["unlocked_since"] = None
        st["attempts_unconfirmed"] = 0
        notified["door_open"] = False
        return "locked", ""
    if r.in_use():
        st["unlocked_since"] = None
        notified["door_open"] = False
        return "in_use", f"engine={r.engine} acc={r.ign3} park={r.park}"
    # unlocked, off, in Park
    if not st.get("unlocked_since"):
        st["unlocked_since"] = iso(r.car_sync or now)
    since = parse_dt(st["unlocked_since"])
    elapsed = (now - since).total_seconds() / 60
    if elapsed < cfg.unlocked_threshold_min:
        return "wait", f"unlocked {elapsed:.0f}m"
    if not r.openings_known():
        return "unknown", f"unlocked {elapsed:.0f}m but a door/trunk/hood state is unknown"
    if r.any_open():
        return "blocked", f"unlocked {elapsed:.0f}m with a door/trunk/hood open"
    if st.get("locks_today", 0) >= cfg.max_lock_attempts_per_day:
        return "cap", f"{st.get('locks_today')} locks today"
    nxt = parse_dt(st.get("next_lock_allowed_at"))
    if nxt and now < nxt:
        return "backoff", f"next lock allowed {iso(nxt)}"
    return "lock", f"unlocked {elapsed:.0f}m, parked, off, closed"


def poll_delay_min(r: Reading | None, st: dict, cfg: Config, now: dt.datetime) -> float:
    if r is None:
        return cfg.poll_active_min
    if r.in_use():
        return cfg.poll_active_min
    if r.car_sync and (now - r.car_sync).total_seconds() / 60 <= cfg.active_window_min:
        return cfg.poll_active_min
    if st.get("unlocked_since"):
        return cfg.poll_active_min
    return cfg.poll_idle_min
