import datetime as dt
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ev9_autolock.core import Config, Reading, decide, poll_delay_min, roll_day  # noqa: E402
from ev9_autolock import service as svcmod  # noqa: E402
from ev9_autolock.util import Paths, RedactFilter, add_secret, redact  # noqa: E402

T0 = dt.datetime(2026, 11, 22, 2, 0, tzinfo=dt.UTC)


HOME = {"name": "garage", "lat": 35.1000, "lon": -89.9000, "radius_m": 150}
NEAR = (35.1005, -89.9000)   # ~56 m north of HOME
FAR = (35.1200, -89.9000)    # ~2.2 km north


def rd(t=T0, sync=None, locked=False, engine=False, ign3=False, park=True, doors=(0, 0, 0, 0), trunk=0, hood=0,
       pos=None, pos_sync=None):
    return Reading(t=t, car_sync=sync or t, locked=locked, engine=engine, ign3=ign3, park=park,
                   doors=list(doors), trunk=trunk, hood=hood,
                   lat=pos[0] if pos else None, lon=pos[1] if pos else None,
                   loc_sync=(pos_sync or sync or t) if pos else None)


class Decide(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(unlocked_threshold_min=5)
        self.st = {}

    def d(self, r, now=None, paused=False):
        return decide(r, self.st, self.cfg, now or r.t, paused)[0]

    def test_locked(self):
        self.assertEqual(self.d(rd(locked=True)), "locked")

    def test_under_threshold_then_over(self):
        self.assertEqual(self.d(rd()), "wait")
        self.assertEqual(self.d(rd(t=T0 + dt.timedelta(minutes=4))), "wait")
        self.assertEqual(self.d(rd(t=T0 + dt.timedelta(minutes=6))), "lock")

    def test_clock_starts_at_car_report_time(self):
        r = rd(t=T0, sync=T0 - dt.timedelta(minutes=30))
        self.assertEqual(self.d(r), "lock")

    def test_in_use_resets(self):
        self.d(rd(sync=T0 - dt.timedelta(minutes=30)))
        for kw in ({"engine": True}, {"ign3": True}, {"park": False}):
            self.st["unlocked_since"] = (T0 - dt.timedelta(minutes=30)).isoformat()
            self.assertEqual(self.d(rd(**kw)), "in_use")
            self.assertIsNone(self.st["unlocked_since"])

    def test_unknowns_never_act(self):
        for kw in ({"locked": None}, {"engine": None}, {"ign3": None}, {"park": None}):
            self.st = {}
            self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1), **kw)), "unknown")
        self.st = {}
        self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1), trunk=None)), "unknown")

    def test_door_open_blocks(self):
        self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1), doors=(1, 0, 0, 0))), "blocked")
        self.st = {}
        self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1), hood=1)), "blocked")

    def test_cap_and_backoff(self):
        self.st = {"locks_today": 3}
        self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1))), "cap")
        self.st = {"next_lock_allowed_at": (T0 + dt.timedelta(minutes=5)).isoformat()}
        self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1))), "backoff")

    def test_pause_and_quiet(self):
        self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1)), paused=True), "paused")
        self.cfg.quiet_hours = "19:00-21:00"  # T0 = 20:00 CST
        self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1))), "quiet")
        self.cfg.quiet_hours_mode = "lock_only"
        self.assertEqual(self.d(rd(sync=T0 - dt.timedelta(hours=1))), "lock")

    def test_daily_reset_local_midnight(self):
        st = {"day": "2026-11-21", "locks_today": 3}
        roll_day(st, self.cfg, dt.datetime(2026, 11, 22, 5, 59, tzinfo=dt.UTC))  # 23:59 CST on the 21st
        self.assertEqual(st["locks_today"], 3)
        roll_day(st, self.cfg, dt.datetime(2026, 11, 22, 6, 1, tzinfo=dt.UTC))   # 00:01 CST on the 22nd
        self.assertEqual(st["locks_today"], 0)

    def test_poll_delay(self):
        self.assertEqual(poll_delay_min(rd(engine=True), {}, self.cfg, T0), 2)
        old = rd(sync=T0 - dt.timedelta(hours=2), locked=True)
        self.assertEqual(poll_delay_min(old, {}, self.cfg, T0), 10)


class Zones(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(unlocked_threshold_min=5)
        self.st = {}
        self.old = T0 - dt.timedelta(minutes=30)

    def d(self, r):
        return decide(r, self.st, self.cfg, r.t, False, [HOME])[0]

    def test_distance(self):
        from ev9_autolock.core import distance_m
        self.assertAlmostEqual(distance_m(*NEAR, HOME["lat"], HOME["lon"]), 55.6, delta=1)
        self.assertAlmostEqual(distance_m(*FAR, HOME["lat"], HOME["lon"]), 2224, delta=5)

    def test_inside_zone_never_locks(self):
        self.assertEqual(self.d(rd(sync=self.old, pos=NEAR)), "in_zone")
        self.assertIsNone(self.st["unlocked_since"])

    def test_outside_zone_locks(self):
        self.assertEqual(self.d(rd(sync=self.old, pos=FAR)), "lock")

    def test_no_position_never_locks(self):
        self.assertEqual(self.d(rd(sync=self.old)), "unknown")

    def test_position_from_before_last_drive_never_locks(self):
        drive = T0 - dt.timedelta(minutes=20)
        self.d(rd(t=drive, sync=drive, engine=True, park=False, pos=FAR))   # seen driving at T0-20
        # parked now, but the car's position is from T0-60 (before that drive)
        r = rd(sync=self.old, pos=FAR, pos_sync=T0 - dt.timedelta(minutes=60))
        self.assertEqual(self.d(r), "unknown")
        # position reported after the drive -> trusted
        r = rd(sync=self.old, pos=FAR, pos_sync=drive + dt.timedelta(minutes=1))
        self.assertEqual(self.d(r), "lock")

    def test_accessory_power_in_park_keeps_position(self):
        acc = T0 - dt.timedelta(minutes=20)
        self.d(rd(t=acc, sync=acc, ign3=True, pos=FAR, pos_sync=T0 - dt.timedelta(minutes=60)))
        r = rd(sync=self.old, pos=FAR, pos_sync=T0 - dt.timedelta(minutes=60))
        self.assertEqual(self.d(r), "lock")

    def test_no_zones_ignores_position(self):
        self.assertEqual(decide(rd(sync=self.old), {}, self.cfg, T0, False, [])[0], "lock")


class FakeClient:
    def __init__(self, readings, lock_result="SUCCESS", lock_effect=True, raise_on_read=None):
        self.readings, self.lock_result, self.lock_effect = list(readings), lock_result, lock_effect
        self.raise_on_read = raise_on_read
        self.locks = 0
        self.forced = 0
        self.locked_now = False

    def ensure(self):
        pass

    def read(self):
        if self.raise_on_read:
            raise self.raise_on_read
        r = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if self.locked_now:
            r = rd(t=r.t, locked=True)
        return r

    def lock(self):
        self.locks += 1
        self.locked_now = self.lock_effect
        return "xid"

    def action_status(self, xid, timeout):
        return self.lock_result

    def force_refresh(self):
        self.forced += 1


class Loop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.paths = Paths(state_dir=d, config=d / "config.toml", secrets=d / "secrets.env")
        self.now = [T0]

    def tearDown(self):
        self.tmp.cleanup()

    def svc(self, client, **cfgkw):
        cfg = Config(**{"dry_run": False, **cfgkw})
        s = svcmod.Service(self.paths, cfg, {"NOTIFY_WEBHOOK_URL": ""}, lambda st, save: client,
                           sleep=lambda x: None, now=lambda: self.now[0])
        return s

    def events(self):
        p = self.paths.events
        return [json.loads(l)["event"] for l in p.read_text().splitlines()] if p.exists() else []

    def test_lock_confirmed(self):
        c = FakeClient([rd(sync=T0 - dt.timedelta(minutes=20))])
        s = self.svc(c)
        s.poll_once()
        self.assertEqual(c.locks, 1)
        self.assertIn("locked", self.events())
        self.assertEqual(s.st["locks_today"], 1)
        self.assertIsNone(s.st["unlocked_since"])

    def test_dry_run_never_locks(self):
        c = FakeClient([rd(sync=T0 - dt.timedelta(minutes=20))])
        s = self.svc(c, dry_run=True)
        s.poll_once()
        s.poll_once()
        self.assertEqual(c.locks, 0)
        self.assertEqual(self.events().count("would_lock"), 1)  # second pass is in the 30-min backoff

    def test_timeout_then_confirms(self):
        c = FakeClient([rd(sync=T0 - dt.timedelta(minutes=20))], lock_result="TIMEOUT")
        self.svc(c).poll_once()
        self.assertIn("locked", self.events())

    def test_never_confirms_backs_off_and_caps(self):
        c = FakeClient([rd(sync=T0 - dt.timedelta(minutes=20))], lock_effect=False)
        s = self.svc(c)
        s.poll_once()
        self.assertEqual(c.forced, 1)
        self.assertIn("lock_unconfirmed", self.events())
        s.poll_once()  # in backoff
        self.assertEqual(c.locks, 1)
        for mins in (11, 45, 120, 240):
            self.now[0] = T0 + dt.timedelta(minutes=mins)
            s.poll_once()
        self.assertLessEqual(c.locks, 3)
        self.assertEqual(c.forced, 1 + (1 if c.locks >= 3 else 0))

    def test_otp_required_idles_and_notifies_once(self):
        from ev9_autolock.kia import OTPRequired
        c = FakeClient([rd()], raise_on_read=OTPRequired("x"))
        s = self.svc(c)
        self.assertEqual(s.poll_once(), 10)
        self.assertEqual(s.poll_once(), 10)
        self.assertEqual(self.events().count("needs_otp"), 1)
        self.assertEqual(s.st["auth_state"], "needs_otp")

    def test_bad_password_exits_78(self):
        from ev9_autolock.kia import BadPassword
        c = FakeClient([rd()], raise_on_read=BadPassword("1001"))
        with self.assertRaises(SystemExit) as cm:
            self.svc(c).poll_once()
        self.assertEqual(cm.exception.code, 78)

    def test_rate_limit_backs_off_60(self):
        class RateLimitingError(Exception):
            pass
        c = FakeClient([rd()], raise_on_read=RateLimitingError("slow down"))
        self.assertEqual(self.svc(c).poll_once(), 60)

    def write_zones(self, text):
        self.paths.zones.write_text(text)

    def test_zone_file_respected(self):
        self.write_zones(json.dumps({"zones": [HOME]}))
        c = FakeClient([rd(sync=T0 - dt.timedelta(minutes=20), pos=NEAR)])
        self.svc(c).poll_once()
        self.assertEqual(c.locks, 0)

    def test_broken_zone_file_never_locks(self):
        self.write_zones("{not json")
        c = FakeClient([rd(sync=T0 - dt.timedelta(minutes=20), pos=FAR)])
        s = self.svc(c)
        s.poll_once()
        s.poll_once()
        self.assertEqual(c.locks, 0)
        self.assertEqual(self.events().count("zones_bad"), 1)

    def test_kia_timestamp(self):
        from ev9_autolock.kia import _kia_utc
        self.assertEqual(_kia_utc("20261122023855.901"), dt.datetime(2026, 11, 22, 2, 38, 55, tzinfo=dt.UTC))
        self.assertIsNone(_kia_utc("garbage"))

    def test_state_file_mode(self):
        s = self.svc(FakeClient([rd(locked=True)]))
        s.poll_once()
        self.assertEqual(oct(self.paths.state.stat().st_mode & 0o777), "0o600")


class Safety(unittest.TestCase):
    def test_no_unlock_call_anywhere(self):
        src = Path(__file__).resolve().parents[1] / "src"
        for f in src.rglob("*.py"):
            self.assertNotRegex(f.read_text(), r"unlock\s*\(", f"{f} mentions an unlock call")

    def test_redaction(self):
        add_secret("hunter2pw")
        msg = 'login bob@example.com sid=ABCDEF rmtoken: XYZ123 "password": "hunter2pw" otpKey=QQQ 1HGCM82633A004352'
        out = redact(msg)
        for bad in ("bob@example.com", "ABCDEF", "XYZ123", "hunter2pw", "QQQ", "1HGCM82633A004352"):
            self.assertNotIn(bad, out)
        rec = logging.LogRecord("x", logging.INFO, "", 0, "pw %s", ("hunter2pw",), None)
        RedactFilter().filter(rec)
        self.assertNotIn("hunter2pw", rec.getMessage())

    def test_token_file_has_no_password(self):
        import datetime as _dt
        from hyundai_kia_connect_api import Token
        from ev9_autolock.kia import load_token, save_token
        with tempfile.TemporaryDirectory() as d:
            p = Paths(state_dir=Path(d), config=Path(d) / "c", secrets=Path(d) / "s")
            save_token(p, Token(username="u@x.com", password="hunter2pw", access_token="S", refresh_token="R",
                                device_id="D", valid_until=_dt.datetime.now(_dt.UTC)))
            self.assertEqual(json.loads(p.token.read_text())["password"], "")
            self.assertEqual(oct(p.token.stat().st_mode & 0o777), "0o600")
            self.assertEqual(load_token(p, "pw2").password, "pw2")


if __name__ == "__main__":
    unittest.main()
