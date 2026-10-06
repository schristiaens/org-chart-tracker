# Org Chart Tracker

Org Chart Tracker reads the employee directory visible to your BambooHR account, saves dated snapshots, and reports new hires and departures with title, department, and manager. It can send daily emails, macOS notifications, and weekly and monthly roll-ups.

## Example email

Plain-text version (HTML mail clients show the same people as a table):

```text
Subject: BambooHR: 1 departure(s), 1 new hire(s) — 2026-10-06

BambooHR org changes for 2026-10-06

1 departure(s), 1 new hire(s)

Departures by department:
    1  Finance

Departures:
  Jordan Rivera — Accountant — Finance (mgr: Pat Lee)

New hires by department:
    1  Engineering

New hires:
  Sam Ortiz — Software Engineer — Engineering (mgr: Alex Kim)
```

## Requirements

- Python 3.9 or newer on macOS or Linux.
- Standard library only; nothing to pip install.
- A BambooHR account that can create an API key and view the employee directory.
- Optional: an SMTP account and app password for email, and `terminal-notifier` for clickable macOS notifications.

## Quick start

On macOS, clone outside Desktop, Documents, Downloads, and iCloud Drive. Scheduled jobs may otherwise fail with `Operation not permitted`.

```bash
cd ~
git clone <repository-url> bhr-org-tracker
cd bhr-org-tracker
python3 onboard.py
```

`onboard.py` checks your BambooHR access, writes `~/.secrets.env`, and offers to test email, take the baseline snapshot, and install the daily schedule. Re-running it is safe; saved values become the defaults.

## Commands

Run from the repository after `. ~/.secrets.env`. Flags below belong to `python3 org_tracker.py` unless marked `onboard.py`.

| Command or option | Purpose |
|---|---|
| `python3 onboard.py` | Interactive setup. |
| `onboard.py --dry-run` / `--secrets-file PATH` | Check without saving, emailing, installing, or taking a snapshot / use another settings file. |
| `--run` | Pull the roster and save a snapshot; with a prior snapshot, compare it and write a report. |
| `--check-auth` / `--test-email` / `--test-notify` | Check credentials / send a sample email / post a sample macOS notification. |
| `--diff-only` | Recompare the two newest snapshots without contacting BambooHR. |
| `--rollup weekly\|monthly\|auto` | Build a stored-snapshot roll-up; `auto` builds reports that are due. |
| `--email` / `--notify` | Email the result / post a macOS notification; use with `--run` or `--rollup`. |
| `--day YYYY-MM-DD` | Override today's date. |
| `-h`, `--help` | Show the complete CLI help. |

## Output files

Files are stored under the repository. To use another folder, set `BHR_ORG_TRACKER_HOME` in your shell before running `onboard.py` or an installer, not in `~/.secrets.env`; the scheduled job ignores the settings-file value.

| Path | Contents |
|---|---|
| `snapshots/snapshot-YYYY-MM-DD.csv` | Full directory snapshot. |
| `reports/changes-YYYY-MM-DD.csv` | Daily hires and departures. |
| `reports/changes-YYYY-MM-DD.html` | Browser report, written with `--notify`. |
| `reports/rollup-{weekly-YYYY-MM-DD,monthly-YYYY-MM}.{csv,html}` | Monday-anchored weekly and previous-month roll-ups. |
| `exports/` | Org-chart PDFs; empty unless you use the session-cookie fallback. |
| `tracker.log` | Run history and errors. |

## Scheduling

- macOS uses a launchd job named `com.bhr-org-tracker`, installed by `install_cron.sh`; the default time is 09:05.
- Linux uses the `bhr-org-tracker.timer` systemd user timer, scheduled for 09:05 with up to five minutes of randomized delay. Run `sudo loginctl enable-linger "$USER"` so it runs while logged out.
- Without `systemctl`, `install_systemd.sh` creates `run_daily.sh` and prints a cron line. Add that line with `crontab -e`; the installer does not add it.

The generated runner performs the daily run and then `--rollup auto`. Re-run onboarding after moving the repository.

## Failure safety

- With email on, a message goes out every day, even with no changes (`BambooHR: no org changes — <date>`). If no email arrives, the job did not run or could not send mail; check `tracker.log`. See “No email at all” under Troubleshooting.
- Once the tracker starts, a failed `--run --email` tries to send a `BambooHR tracker FAILED` message and exits nonzero instead of looking like a no-change day.
- A roster below 50% of the previous headcount is refused before a snapshot is written. If the drop is real, accept it once: `BHR_ALLOW_SHRINK=1 python3 org_tracker.py --run`. Do not put the override in the settings file; that disables the check for every run.
- The first `--run` saves a baseline only. Change reporting starts with the next run.

## Privacy

- Check company policy before copying employee-directory data to this computer or email.
- The API key sees only employees and fields that the BambooHR account can see.
- Settings, snapshots, and reports are written with mode `0600`; data directories are created with mode `0700`. `tracker.log` uses your default permissions; it holds counts and email addresses, not names.
- Emails name only people who changed; the full roster is never emailed.
- macOS notifications contain counts and department names, never employee names.
- Onboarding backs up an existing settings file to `~/.secrets.env.bak` (or `PATH.bak` with `--secrets-file PATH`; `0600`, old secrets included). Delete it once the new settings work.

## Troubleshooting

Start with `tail -n 50 tracker.log`.

| Problem | Action |
|---|---|
| `BHR_SUBDOMAIN is not set` or `No roster source is configured` | Load `. ~/.secrets.env`; confirm the subdomain and API key are present. |
| API `401`, `403`, `404`, or an empty directory | Replace a bad or revoked key, correct the subdomain, or ask the BambooHR administrator about directory/API access. Then run `--check-auth`. |
| `roster dropped from N to M (>50%)` | Confirm the change is real before using the one-off override shown above. |
| `email not configured` or SMTP login fails | Set all three required email values and use an app password; verify the host and port. |
| No email at all, not even a failure email | On macOS, use `launchctl list \| grep bhr-org-tracker`. On Linux, use `sh install_systemd.sh status` and check linger. |
| `Operation not permitted` on macOS | Move the clone outside Desktop, Documents, Downloads, and iCloud Drive, then rerun onboarding. |
| Notification is absent or not clickable | Run `brew install terminal-notifier`, then `python3 org_tracker.py --test-notify` once (it registers the app; on Intel Macs the installer does not), and allow it in System Settings > Notifications. |

## Manual setup

Use `.env.example` as the source of truth, copy the needed `export` lines to `~/.secrets.env`, and set its mode to `0600`. See `python3 org_tracker.py --help` for every option.

| Variable | Default / notes |
|---|---|
| `BHR_SUBDOMAIN` | Required; company subdomain, hostname, or full BambooHR URL. |
| `BHR_PERSONAL_API_KEY` | Required for the supported API path. |
| `BHR_SMTP_USER` | Required for email. |
| `BHR_SMTP_PASSWORD` | Required for email; use an app password. |
| `BHR_MAIL_TO` | Required for email; comma-separated addresses are accepted. |
| `BHR_SMTP_HOST` | `smtp.gmail.com`. |
| `BHR_SMTP_PORT` | `465`; any other port, normally `587`, uses STARTTLS. |
| `BHR_MAIL_FROM` | `BHR_SMTP_USER`. |
| `BHR_MAIL_ATTACH_CSV` | Off; set to `1` to attach the change-report CSV. |

Then verify credentials, email, and the baseline, and install the schedule:

```bash
. ~/.secrets.env
python3 org_tracker.py --check-auth
python3 org_tracker.py --test-email
python3 org_tracker.py --run
sh install_cron.sh                 # macOS
sh install_systemd.sh              # Linux
sudo loginctl enable-linger "$USER" # Linux; run once
```

## Tests

```bash
python3 test_tracker.py
python3 test_onboard.py
```

## Uninstall

1. Remove the schedule with `sh install_cron.sh remove` on macOS or `sh install_systemd.sh remove` on Linux. For plain cron, delete its line with `crontab -e`.
2. Remove the `BHR_*` lines from `~/.secrets.env`, plus `~/.secrets.env.bak` if present. Delete the whole settings file only if nothing else uses it.
3. Revoke the BambooHR API key and any mail-provider app password.
4. Delete the repository and `BHR_ORG_TRACKER_HOME` if you set one.

## Experimental session-cookie fallback

`BHR_SESSION_COOKIE` with `--root ID` falls back to BambooHR's web PDF exports (needs `pdftotext`). Unsupported: cookies expire and the PDF parser is company-specific.
