# Kia auto-lock (ev9-autolock)

Many Kias have no walk-away auto-lock. The Kia Connect app warns you when the car is left unlocked, but it
won't lock the car by itself. This small Linux service does: it watches the car through Kia Connect and
**locks it when it has been left unlocked, parked and powered off for a few minutes.**

- **Lock only.** No code path can unlock the car (a test fails if `unlock(` appears anywhere in the source).
- **Never acts on guesses.** It locks only when the car reports *all* of: unlocked, in Park, powered off
  (including accessory power), every door, trunk and hood closed, for `unlocked_threshold_min` (default 5).
  If any value is missing it does nothing.
- **Gentle on the car and on Kia's servers.** It reads Kia's cached status (doesn't wake the car), every
  5 minutes while the car is active and every 10 while it sleeps. At most 8 lock commands and 4 sign-ins a
  day. It never requests a one-time code by itself and never retries a wrong password.
- **Dry run first.** It ships with `dry_run = true`: it only logs `would_lock` until you turn that off.

> **Disclaimer.** Not affiliated with or endorsed by Kia. It uses Kia's unofficial cloud API through the
> community library [hyundai_kia_connect_api](https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api).
> Kia can change or block that API at any time, and using it may be against Kia Connect's terms. Use at your
> own risk; see LICENSE (no warranty).

## What it has been tested on

A **2026 Kia EV9, USA, Kia Connect** account. On that car, unlocking with the fob, driving and parking all
showed up in Kia's cached status within about a minute, and remote locks took 6 s (car awake) to 80 s (car
asleep).

Other **US Kia** models probably work. The "in Park" (`transCond`) and accessory-power (`ign3`) fields were
only confirmed on the EV9. A car that doesn't report them is treated as *unknown*, so the service does
nothing rather than lock at the wrong time. Hyundai, Genesis and other regions use different code paths in
the library and are **not supported** without changes (pull requests welcome).

## Things to know before you turn it on

- **The alarm.** Locking remotely usually arms the theft alarm. Someone waiting in a switched-off car
  (passenger, kid, dog) who opens a door afterwards may set it off. Use `ev9-autolock pause` for those times.
- **Sign-in codes.** Kia USA asks for a one-time code (email/SMS) the first time, then remembers the device for
  an unknown time. When it expires the service stops locking, logs `needs_otp`, and you rerun
  `sudo ev9-autolock setup-otp`. Set up a notification (below) so you hear about it.
- **Limits.** Kia doesn't publish its limits. The defaults stay far below the only community figure
  (about 30 commands a day).
- **The library changes often.** It is pinned in `requirements.lock`. Upgrade on purpose, then test in dry run.

## Install (Debian/Ubuntu with systemd, Python 3.12+)

A Raspberry Pi or any always-on Linux box works, as long as it has **Python 3.12 or newer** (the Kia
library needs it): Ubuntu 24.04+, Debian 13, or Raspberry Pi OS based on Debian 13 ("Trixie"). The older
Raspberry Pi OS "Bookworm" has Python 3.11 and won't work. Check with `python3 --version`.

```sh
sudo apt install git python3-venv
git clone https://github.com/ndavidovics/kia-autolock.git
cd kia-autolock
sudo ./install.sh
```

The installer takes under a minute, is safe to re-run (it keeps your settings, login and zones), and
doesn't start anything until you enable the service.

Then:

```sh
sudo nano /etc/ev9-autolock/secrets.env     # KIA_USERNAME / KIA_PASSWORD (your Kia Connect login)
sudo ev9-autolock setup-otp                 # enter the one-time code Kia sends you
sudo ev9-autolock status                    # check it matches the Kia app
sudo systemctl enable --now ev9-autolock    # starts in dry run
```

Park, leave the car unlocked for more than 5 minutes, and check `sudo ev9-autolock events`: you should see a
`would_lock`. When you're happy, set `dry_run = false` in `/etc/ev9-autolock/config.toml` and run
`sudo systemctl restart ev9-autolock`.

## No-lock zones (e.g. your garage)

Kia's cached status includes the car's last GPS position, so you can mark places where it should never
lock. Park the car there, then:

```sh
sudo ev9-autolock zone add garage              # uses the car's current position, radius 150 m
sudo ev9-autolock zone add garage --radius 50  # tighter, if you park on the street nearby too
sudo ev9-autolock zone add work --lat 35.1495 --lon -90.0490 --radius 200
sudo ev9-autolock zone list
sudo ev9-autolock zone remove work
```

The command prints a map link so you can check the spot. Zones live in `/etc/ev9-autolock/zones.json`, and
the service reads it on every check (no restart needed).

Once you have any zone, the service locks only when it **knows** the car is outside all of them. If the car
reports no position, or the position is older than the last time the car left Park, it does nothing (logged
as `unknown`). Garages often block GPS. The car usually keeps its last fix from the driveway, which is why
the default radius is 150 m. The position is never written to the logs or readings, only whether one was
reported and when.

## Everyday commands

```sh
sudo ev9-autolock status           # current reading and service state
sudo ev9-autolock events -n 30     # would_lock, locked, lock_unconfirmed, needs_otp, ...
sudo ev9-autolock pause            # stop locking (service keeps running); resume to undo
journalctl -u ev9-autolock -f      # one line per check
```

## Notifications (optional)

Put a webhook in `NOTIFY_WEBHOOK_URL` in `secrets.env`. The easiest is [ntfy](https://ntfy.sh): install the
app, subscribe to a hard-to-guess topic, and set `NOTIFY_WEBHOOK_URL=https://ntfy.sh/<your-topic>`. Any other
URL gets a JSON POST (`{"event", "time", "vehicle", "detail"}`), e.g. a Slack or Discord incoming webhook.
Events: `locked`, `would_lock`, `lock_unconfirmed`, `door_open_long`, `cap_reached`, `needs_otp`,
`auth_failed`, `login_cap`, `zones_bad`.

## Configuration

`/etc/ev9-autolock/config.toml` (restart after editing):

| Key | Default | Meaning |
| --- | --- | --- |
| `unlocked_threshold_min` | 5 | Minutes parked, off, closed and unlocked before it locks |
| `poll_active_min` / `poll_idle_min` | 5 / 10 | Check interval while the car is active / asleep |
| `max_lock_attempts_per_day` | 8 | Hard cap on lock commands |
| `lock_confirm_timeout_s` | 150 | How long to wait for the car to confirm |
| `quiet_hours` | "" | e.g. `"23:00-06:00"`; `quiet_hours_mode` = `skip` or `lock_only` |
| `timezone` | America/Chicago | For quiet hours and the daily cap |
| `vehicle_vin_suffix` | "" | Pick a car when the account has several |
| `dry_run` | true | Log only |

## How it decides

Every check: read the cached status. Locked → reset. Car on, accessory power on, or not in Park → reset (the
car is in use). Inside a no-lock zone → reset; zones set but position unknown or stale → wait. Otherwise, start a clock at the time the car reported being unlocked. Past the threshold,
with everything closed and known, send one lock, wait for the car's answer, re-read the status to confirm,
and back off (10 min, 30 min, then stop for the day) if it didn't take.

## Files

| Path | What |
| --- | --- |
| `/opt/ev9-autolock` | code and its virtualenv |
| `/etc/ev9-autolock/config.toml` | settings |
| `/etc/ev9-autolock/zones.json` | no-lock zones (managed with `ev9-autolock zone`) |
| `/etc/ev9-autolock/secrets.env` | login and webhook, root-only (0600) |
| `/var/lib/ev9-autolock` | saved sign-in (password removed), state, readings, events |

The service runs as its own unprivileged user (`ev9lock`) with systemd sandboxing. Logs never include your
password, session tokens, email or VIN.

## Development

From the cloned folder (the installer doesn't copy the tests):

```sh
/opt/ev9-autolock/venv/bin/python -m unittest tests/test_service.py
```

The tests use a fake car; nothing touches a real account.

## Uninstall

```sh
sudo systemctl disable --now ev9-autolock
sudo rm -rf /etc/systemd/system/ev9-autolock.service /opt/ev9-autolock /etc/ev9-autolock /var/lib/ev9-autolock /usr/local/bin/ev9-autolock
sudo userdel ev9lock
```
