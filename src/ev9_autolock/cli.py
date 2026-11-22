"""ev9-autolock: lock a Kia EV9 that has been left unlocked. Lock only; never unlocks."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .core import distance_m, where
from .service import EXIT_CONFIG, Service, load_config, load_secrets, load_zones, setup_logging
from .util import Paths, redact, write_atomic

PATHS = Paths(
    state_dir=Path(os.environ.get("STATE_DIRECTORY", "/var/lib/ev9-autolock")),
    config=Path(os.environ.get("EV9_CONFIG", "/etc/ev9-autolock/config.toml")),
    secrets=Path(os.environ.get("EV9_SECRETS", "/etc/ev9-autolock/secrets.env")),
)


def _client_factory(cfg, secrets):
    from .kia import KiaClient

    def make(st, save):
        return KiaClient(cfg, secrets["KIA_USERNAME"], secrets["KIA_PASSWORD"], PATHS, st, save)
    return make


def _need_secrets(secrets):
    if not secrets["KIA_USERNAME"] or not secrets["KIA_PASSWORD"]:
        print(f"Missing KIA_USERNAME / KIA_PASSWORD (environment or {PATHS.secrets}).", file=sys.stderr)
        sys.exit(EXIT_CONFIG)


def _chown_state(path: Path) -> None:
    """When run as root, hand files to the service user."""
    if os.geteuid() == 0:
        import pwd
        try:
            pw = pwd.getpwnam("ev9lock")
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except KeyError:
            pass


def cmd_setup_otp(cfg, secrets):
    from hyundai_kia_connect_api import VehicleManager
    from .kia import OTP_NOTIFY_TYPE, OTPRequest, AuthenticationError, save_token

    _need_secrets(secrets)
    vm = VehicleManager(region=cfg.region, brand=cfg.brand, username=secrets["KIA_USERNAME"],
                        password=secrets["KIA_PASSWORD"], pin="")
    try:
        res = vm.login()
    except AuthenticationError as e:
        sys.exit("Login refused: " + redact(e) + " (not retrying)")
    if isinstance(res, OTPRequest):
        opts = [o for o in (("e", OTP_NOTIFY_TYPE.EMAIL, "email", res.has_email),
                            ("s", OTP_NOTIFY_TYPE.SMS, "text message", res.has_sms)) if o[3]]
        if not opts:
            sys.exit("Kia asked for a code but offered no way to send it.")
        choice = opts[0]
        if len(opts) > 1:
            a = input("Send the code by (e)mail or (s)ms? ").strip().lower()[:1]
            choice = next((o for o in opts if o[0] == a), opts[0])
        vm.send_otp(choice[1])
        code = input(f"Code sent by {choice[2]}. Enter it once: ").strip()
        try:
            vm.verify_otp_and_complete_login(code)
        except Exception as e:
            sys.exit("Code not accepted: " + redact(e) + "\nWait a few minutes before trying again.")
    elif res is not True:
        sys.exit("Login failed.")
    save_token(PATHS, vm.token)
    _chown_state(PATHS.token)
    print("Saved. The service picks it up within 10 minutes.")


def cmd_status(cfg, secrets):
    _need_secrets(secrets)
    svc = Service(PATHS, cfg, secrets, _client_factory(cfg, secrets))
    c = svc.get_client()
    r = c.read()
    svc.state.save()
    _chown_state(PATHS.state)
    _chown_state(PATHS.token)
    _chown_state(PATHS.readings)
    b = lambda x: {True: "yes", False: "no", None: "?"}.get(x, str(x))
    print(f"{'LOCKED' if r.locked else 'UNLOCKED' if r.locked is False else 'UNKNOWN'} | reported {r.car_sync} | "
          f"on {b(r.engine)} acc {b(r.ign3)} in Park {b(r.park)} | open: doors {sum(1 for d in r.doors if d)} "
          f"trunk {b(r.trunk)} hood {b(r.hood)} | plugged {b(r.plugged)} charging {b(r.charging)} | {r.soc}%")
    st = svc.st
    zones = load_zones(PATHS)
    if zones:
        kind, name = where(r, st, zones)
        near = ""
        if r.lat is not None:
            d, zn = min((distance_m(r.lat, r.lon, z["lat"], z["lon"]), z["name"]) for z in zones)
            near = f" ({zn} {d:,.0f} m away)"
        print({"zone": f"in no-lock zone {name!r}{near}: won't lock here",
               "outside": f"outside all no-lock zones{near}",
               "unknown": f"position unknown ({name}): won't lock until it is known"}[kind])
    print(f"dry_run={cfg.dry_run} paused={PATHS.pause.exists()} locks_today={st.get('locks_today', 0)} "
          f"auth={st.get('auth_state')} unlocked_since={st.get('unlocked_since')}")


def _save_zones(zones: list) -> None:
    write_atomic(PATHS.zones, json.dumps({"zones": zones}, indent=1) + "\n")
    if os.geteuid() == 0:
        import grp
        try:
            os.chown(PATHS.zones, 0, grp.getgrnam("ev9lock").gr_gid)
            os.chmod(PATHS.zones, 0o640)
        except KeyError:
            pass


def _here(cfg, secrets):
    """The car's position from one cached read, only if it is parked and the position is current."""
    _need_secrets(secrets)
    svc = Service(PATHS, cfg, secrets, _client_factory(cfg, secrets))
    r = svc.get_client().read()
    svc.state.save()
    for f in (PATHS.state, PATHS.token, PATHS.readings):
        _chown_state(f)
    return r, svc.st


def cmd_zone(cfg, secrets, a):
    zones = load_zones(PATHS)
    if a.zcmd == "list":
        if not zones:
            print("no no-lock zones (it may lock anywhere)")
        for z in zones:
            print(f"{z['name']:<16} radius {z.get('radius_m', 150):.0f} m   {z['lat']:.6f},{z['lon']:.6f}")
        return
    if a.zcmd == "remove":
        left = [z for z in zones if z["name"] != a.name]
        if len(left) == len(zones):
            print(f"no zone named {a.name!r}", file=sys.stderr)
            sys.exit(1)
        _save_zones(left)
        print(f"removed {a.name!r}" + ("" if left else "; no zones left, so it may lock anywhere again"))
        return
    # add
    if not (10 <= a.radius <= 5000):
        print("radius must be 10-5000 m", file=sys.stderr)
        sys.exit(1)
    if (a.lat is None) != (a.lon is None):
        print("give both --lat and --lon, or neither (= where the car is now)", file=sys.stderr)
        sys.exit(1)
    if a.lat is not None:
        lat, lon = a.lat, a.lon
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            print("bad coordinates", file=sys.stderr)
            sys.exit(1)
    else:
        r, st = _here(cfg, secrets)
        kind, why = where(r, st, [{"name": "_", "lat": 0, "lon": 0, "radius_m": 1}])
        if kind == "unknown":
            print(f"can't use the car's position: {why}. Try again after it reports, or pass --lat/--lon.", file=sys.stderr)
            sys.exit(1)
        if r.in_use():
            print("the car is on or not in Park; park it where the zone should be first", file=sys.stderr)
            sys.exit(1)
        lat, lon = r.lat, r.lon
        print(f"car position reported {r.loc_sync:%Y-%m-%d %H:%M} UTC")
    zones = [z for z in zones if z["name"] != a.name] + [
        {"name": a.name, "lat": round(lat, 6), "lon": round(lon, 6), "radius_m": a.radius}]
    _save_zones(zones)
    print(f"saved no-lock zone {a.name!r}: {lat:.6f},{lon:.6f} radius {a.radius:.0f} m")
    print(f"check it: https://www.google.com/maps?q={lat:.6f},{lon:.6f}")
    print("the service picks it up on its next check; it now locks only when the car is known to be outside every zone")


def cmd_events(n: int):
    if not PATHS.events.exists():
        print("no events yet")
        return
    for line in PATHS.events.read_text().splitlines()[-n:]:
        print(line)


def main(argv=None):
    p = argparse.ArgumentParser(prog="ev9-autolock", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup-otp", help="interactive: sign in with a one-time code and save the sign-in")
    sub.add_parser("status", help="one cached read, printed")
    r = sub.add_parser("run", help="the service loop")
    r.add_argument("--once", action="store_true")
    sub.add_parser("pause", help="stop locking (service keeps running)")
    sub.add_parser("resume", help="undo pause")
    z = sub.add_parser("zone", help="no-lock zones (e.g. your garage)")
    zs = z.add_subparsers(dest="zcmd", required=True)
    za = zs.add_parser("add", help="add/replace a zone at the car's current position (or --lat/--lon)")
    za.add_argument("name")
    za.add_argument("--radius", type=float, default=150, help="metres (default 150)")
    za.add_argument("--lat", type=float)
    za.add_argument("--lon", type=float)
    zs.add_parser("list")
    zr = zs.add_parser("remove")
    zr.add_argument("name")
    e = sub.add_parser("events", help="recent events")
    e.add_argument("-n", type=int, default=20)
    a = p.parse_args(argv)

    cfg = load_config(PATHS)
    setup_logging(cfg.log_level)
    if a.cmd == "pause":
        PATHS.pause.touch()
        _chown_state(PATHS.pause)
        print("paused: no locks until `ev9-autolock resume`")
        return
    if a.cmd == "resume":
        PATHS.pause.unlink(missing_ok=True)
        print("resumed")
        return
    if a.cmd == "events":
        return cmd_events(a.n)
    secrets = load_secrets(PATHS)
    if a.cmd == "setup-otp":
        return cmd_setup_otp(cfg, secrets)
    if a.cmd == "status":
        return cmd_status(cfg, secrets)
    if a.cmd == "zone":
        return cmd_zone(cfg, secrets, a)
    if a.cmd == "run":
        _need_secrets(secrets)
        Service(PATHS, cfg, secrets, _client_factory(cfg, secrets)).run(once=a.once)


if __name__ == "__main__":
    main()
