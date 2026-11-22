"""ev9-autolock: lock a Kia EV9 that has been left unlocked. Lock only; never unlocks."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .service import EXIT_CONFIG, Service, load_config, load_secrets, setup_logging
from .util import Paths, redact

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
    print(f"dry_run={cfg.dry_run} paused={PATHS.pause.exists()} locks_today={st.get('locks_today', 0)} "
          f"auth={st.get('auth_state')} unlocked_since={st.get('unlocked_since')}")


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
    if a.cmd == "run":
        _need_secrets(secrets)
        Service(PATHS, cfg, secrets, _client_factory(cfg, secrets)).run(once=a.once)


if __name__ == "__main__":
    main()
