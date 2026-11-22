"""Thin adapter over hyundai_kia_connect_api. The only car command it exposes is lock."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

from hyundai_kia_connect_api import Token, VehicleManager
from hyundai_kia_connect_api.ApiImpl import OTPRequest
from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE
from hyundai_kia_connect_api.exceptions import AuthenticationError

from .core import Reading
from .util import Paths, add_secret, append_jsonl, redact, utcnow, write_atomic

logging.getLogger("hyundai_kia_connect_api").setLevel(logging.CRITICAL)  # logs bodies and session ids

__all__ = ["KiaClient", "OTPRequired", "BadPassword", "AuthenticationError", "OTPRequest", "OTP_NOTIFY_TYPE"]


class OTPRequired(Exception):
    pass


class BadPassword(Exception):
    pass


def _fp(tok: Token | None) -> str:
    if not tok:
        return ""
    return hashlib.sha256(f"{tok.access_token}|{tok.refresh_token}".encode()).hexdigest()[:12]


def save_token(paths: Paths, tok: Token) -> None:
    d = tok.to_dict()
    d["password"] = ""  # never store the password in the token file
    add_secret(tok.access_token, tok.refresh_token)
    write_atomic(paths.token, json.dumps(d, default=str))


def load_token(paths: Paths, password: str) -> Token | None:
    try:
        d = json.loads(paths.token.read_text())
    except FileNotFoundError:
        return None
    d["password"] = password
    tok = Token.from_dict(d)
    add_secret(tok.access_token, tok.refresh_token)
    return tok


def _status_dict(state: dict) -> dict:
    return ((state.get("lastVehicleInfo") or {}).get("vehicleStatusRpt") or {}).get("vehicleStatus") or {}


class KiaClient:
    def __init__(self, cfg, username: str, password: str, paths: Paths, st: dict, save_state):
        self.cfg, self.paths, self.st, self._save_state = cfg, paths, st, save_state
        self.username, self.password = username, password
        tok = load_token(paths, password)
        if tok is None:
            raise OTPRequired("no saved sign-in")
        self.vm = VehicleManager(region=cfg.region, brand=cfg.brand, username=username,
                                 password=password, pin="", token=tok)
        self.vehicle = None

    # -- sign-in bookkeeping -------------------------------------------
    def _after_call(self, before: str) -> None:
        if _fp(self.vm.token) != before:
            self.st["logins_today"] = self.st.get("logins_today", 0) + 1
            self.st["last_login_at"] = utcnow().isoformat(timespec="seconds")
            save_token(self.paths, self.vm.token)
            self._save_state()
            logging.getLogger("ev9").info("event=session_renewed logins_today=%s", self.st["logins_today"])

    def _guard(self, fn, *a, **kw):
        before = _fp(self.vm.token)
        try:
            return fn(*a, **kw)
        except AuthenticationError as e:
            if "Invalid Email or Password" in str(e) or "1001" in str(e):
                raise BadPassword(redact(e)) from None
            raise OTPRequired(redact(e)) from None
        except AttributeError as e:
            # the library's silent session repair got an OTP request instead of a session
            if "OTPRequest" in str(e) or "access_token" in str(e):
                raise OTPRequired(redact(e)) from None
            raise
        finally:
            self._after_call(before)

    def ensure(self) -> None:
        self._guard(self.vm.check_and_refresh_token)
        if not self.vm.vehicles:
            self._guard(self.vm.initialize_vehicles)
        if self.vehicle is None:
            cars = list(self.vm.vehicles.values())
            suf = (self.cfg.vehicle_vin_suffix or "").upper()
            if suf:
                cars = [c for c in cars if (c.VIN or "").upper().endswith(suf)]
            if len(cars) != 1:
                raise SystemExit(f"expected exactly one matching car, found {len(cars)}; set vehicle_vin_suffix")
            self.vehicle = cars[0]

    # -- reads ----------------------------------------------------------
    def read(self) -> Reading:
        """One cached read (Kia's cloud copy; never wakes the car)."""
        api, v = self.vm.api, self.vehicle
        state = self._guard(lambda: api._get_cached_vehicle_state(token=self.vm.token, vehicle=v))
        api._update_vehicle_properties(v, state)
        vs = _status_dict(state)
        now = utcnow()
        sync = v.last_updated_at
        if sync is not None:
            sync = sync.astimezone(dt.UTC)
        lat = lon = loc_sync = None
        loc = (state.get("lastVehicleInfo") or {}).get("location") if isinstance(state, dict) else None
        if isinstance(loc, dict):
            co = loc.get("coord") or {}
            try:
                lat, lon = float(co["lat"]), float(co["lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                    lat = lon = None
            except (KeyError, TypeError, ValueError):
                lat = lon = None
            loc_sync = _kia_utc((loc.get("syncDate") or {}).get("utc"))
        r = Reading(
            t=now, car_sync=sync, locked=v.is_locked, engine=v.engine_is_running,
            ign3=vs.get("ign3"), park=vs.get("transCond"),
            doors=[v.front_left_door_is_open, v.front_right_door_is_open, v.back_left_door_is_open, v.back_right_door_is_open],
            trunk=v.trunk_is_open, hood=v.hood_is_open, charging=v.ev_battery_is_charging,
            plugged=v.ev_battery_is_plugged_in, soc=v.ev_battery_percentage,
            lat=lat, lon=lon, loc_sync=loc_sync,
        )
        append_jsonl(self.paths.readings, {
            "t": r.t.isoformat(timespec="seconds"), "car_sync": sync.isoformat(timespec="seconds") if sync else None,
            "locked": r.locked, "engine": r.engine, "acc": r.ign3, "park": r.park, "doors": r.doors,
            "trunk": r.trunk, "hood": r.hood, "charging": r.charging, "plugged": r.plugged, "soc": r.soc,
            "pos": lat is not None, "pos_sync": loc_sync.isoformat(timespec="seconds") if loc_sync else None})
        return r

    def force_refresh(self) -> None:
        """Wakes the car. Used at most once per force_refresh_min_interval_min, only to confirm a lock."""
        self._guard(self.vm.force_refresh_vehicle_state, self.vehicle.id)

    # -- the one command --------------------------------------------------
    def lock(self) -> str:
        return self._guard(self.vm.lock, self.vehicle.id)

    def action_status(self, xid: str, timeout: int) -> str:
        st = self._guard(self.vm.check_action_status, self.vehicle.id, xid, synchronous=True, timeout=timeout)
        return getattr(st, "name", str(st))


def _kia_utc(s):
    """Kia's 'YYYYMMDDHHMMSS[.fff]' UTC stamp -> aware datetime, or None."""
    if not s:
        return None
    try:
        return dt.datetime.strptime(str(s)[:14], "%Y%m%d%H%M%S").replace(tzinfo=dt.UTC)
    except ValueError:
        return None
