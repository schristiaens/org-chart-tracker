# bhr-org-tracker

A small script that tells you every morning who joined your company and who left, with each person's job title, department and manager. It reads the employee directory from BambooHR using your own API key, saves a dated snapshot, and compares it to the previous one.

It's for people who work at a company that uses BambooHR and want a daily heads-up on org changes without clicking through the directory. You don't need to be a BambooHR admin; the script sees exactly what your account can see in the BambooHR employee directory, and nothing more.

It's plain Python using only the standard library (`org_tracker.py`, plus `onboard.py` for setup), so there's nothing to `pip install`.

## What you get

A daily email. Its subject line leads with the numbers:

```
BambooHR: 1 departure(s), 2 new hire(s) — 2026-10-06
```

The body groups changes by department and lists each person:

```
BambooHR org changes for 2026-10-06

1 departure(s), 2 new hire(s)

Departures by department:
    1  Finance

Departures:
  Jordan Rivera — Senior Accountant — Finance (mgr: Pat Lee)

New hires by department:
    1  Engineering
    1  Sales

New hires:
  Sam Ortiz — Software Engineer — Engineering (mgr: Alex Kim)
  Riley Chen — Account Executive — Sales (mgr: Morgan Diaz)
```

The email comes as both plain text and HTML (the HTML version is a table). On days with no changes you still get one, titled `BambooHR: no org changes — <date>`. That's deliberate: if the email ever stops arriving, something is broken.

On top of the daily email you get a weekly roll-up every Monday and a monthly one at the start of each month, each with net headcount:

```
BambooHR weekly: 4 departure(s), 6 new hire(s), headcount 412→414 (+2) — week ending 2026-10-05
BambooHR monthly: 11 departure(s), 9 new hire(s), headcount 416→414 (-2) — September 2026
```

On a Mac you can also get a desktop notification. It shows counts and department names only (for example "1 departure(s), 2 new hire(s)" and "Most affected: Finance 1"), never individual names, and clicking it opens the full report.

Email and notifications are both optional. Without them, the script still writes a CSV of each day's changes to disk, plus an HTML page for each weekly and monthly roll-up.

## Requirements

- **Python 3.9 or newer.** macOS includes `python3` with the Xcode Command Line Tools; the first time you type `python3` on a new Mac it may offer to install them. Most Linux distributions ship it already.
- **macOS or Linux.** The scheduling scripts don't support Windows.
- **A BambooHR account at your company that can create an API key** and can see the employee directory. Some companies turn off API keys for regular employees. If yours has, this tool won't work for you, and the person to ask is your BambooHR admin.
- **Optional:** an email account that can send through SMTP with an app password (Gmail, Fastmail and most others work).
- **Optional, macOS only:** [terminal-notifier](https://github.com/julienXX/terminal-notifier) for clickable notifications (`brew install terminal-notifier`).

## Quick start

```bash
cd ~
git clone <repository-url> bhr-org-tracker
cd bhr-org-tracker
python3 onboard.py
```

Clone it somewhere outside Desktop, Documents and Downloads (your home folder is fine). On a Mac, privacy protection usually stops a scheduled job from reading files in those folders or in iCloud-synced ones, and the daily run then fails with `Operation not permitted`.

`onboard.py` walks you through setup one step at a time and explains where to find each thing it asks for. It will:

1. Check your Python version and operating system, explain what data the tool reads and keeps, and ask whether to continue.
2. Ask for your BambooHR subdomain and API key. The key is typed without being shown on screen.
3. Make one test call to BambooHR and report how many people it can see (counts only, no names). If the call fails, it shows the tracker's own explanation and lets you re-enter the name and key and try again.
4. Optionally set up email: pick Gmail, Fastmail, Outlook.com or Microsoft 365 (the servers in the table under [Getting an app password](#getting-an-app-password-for-email)), or enter your own server, then send a test message.
5. On macOS, mention terminal-notifier for clickable notifications, and install it with Homebrew only if you say yes.
6. Save everything to `~/.secrets.env`, readable only by you (mode `0600`).
7. Optionally take the first snapshot right away. Reporting starts with the run after that.
8. Optionally schedule the daily run at a time you pick (default 09:05). On Linux without systemd, the installer prepares the job and prints a crontab line for you to add; nothing runs until you add it, and the summary says so. On macOS, the installer also registers terminal-notifier for clickable notifications if Homebrew installed it under `/opt/homebrew` (Apple silicon). On an Intel Mac, run `open "$(brew --prefix)/opt/terminal-notifier/terminal-notifier.app"` once yourself.
9. Print a summary and the commands you're most likely to want next.

You can skip any optional step and come back to it later. Running `onboard.py` again is safe: it reads your saved settings and offers them as defaults (secrets shown masked, so you only see the last few characters), and it only touches the `BHR_*` lines it manages. Anything else in the file stays as it was. A setting you wrote as a shell expression, such as `export BHR_PERSONAL_API_KEY="$(cat ~/.bhr-key)"`, is never run or rewritten by setup: it is reported as "kept as written". If the subdomain or key is one, the connection check is skipped and setup prints the command to run it yourself; if any email setting is one, the email step leaves email alone, and if that leaves the address, app password or recipient unset, the summary says email is not set up and names the lines to add by hand.

Before changing an existing file, `onboard.py` copies it to `~/.secrets.env.bak`, or to `PATH.bak` if you passed `--secrets-file PATH` (mode `0600`, replaced on each save). That copy holds your secrets too, so delete it once you're happy with the new settings, and always after replacing a key.

| Option | What it does |
|---|---|
| `--dry-run` | Goes through every prompt, and still makes the BambooHR connection check, but writes nothing, sends no test email, installs nothing, and doesn't take a snapshot. Prints what it would have done. |
| `--secrets-file PATH` | Save settings somewhere other than `~/.secrets.env`. `BHR_SECRETS_FILE` in your environment does the same. |
| `-h`, `--help` | Show usage. |

The script needs an interactive terminal and stops with a message if you pipe input into it. Pressing Ctrl-C at any prompt exits without leaving a half-written file.

If you'd rather set things up by hand, read on.

## Manual setup

### Settings

All configuration comes from environment variables. The scheduled job reads them from a secrets file (`~/.secrets.env` by default) each time it runs, so put them there as `export` lines. `.env.example` in this folder is a starting point.

```bash
cp .env.example ~/.secrets.env
chmod 600 ~/.secrets.env
# then edit ~/.secrets.env and fill in your values
```

If you already have a `~/.secrets.env` for other tools, add the `BHR_*` lines to it instead of overwriting it.

The email lines in `.env.example` come commented out, so a copy of it leaves email off. To turn email on, uncomment `BHR_SMTP_USER`, `BHR_SMTP_PASSWORD` and `BHR_MAIL_TO` and fill in real values. Don't uncomment them with placeholders still in place: the script treats any value as a real setting, and every scheduled run would then try to send, fail, and exit with an error.

| Variable | Needed for | Default | Notes |
|---|---|---|---|
| `BHR_SUBDOMAIN` | everything | none | Your company's BambooHR address. `acme`, `acme.bamboohr.com` and `https://acme.bamboohr.com/` all work. |
| `BHR_PERSONAL_API_KEY` | everything | none | Your BambooHR API key (see below). |
| `BHR_SMTP_USER` | email | none | Login for your mail server, usually your full email address. |
| `BHR_SMTP_PASSWORD` | email | none | An app password, not your normal account password. |
| `BHR_MAIL_TO` | email | none | Who gets the reports. Separate several addresses with commas. |
| `BHR_SMTP_HOST` | email | `smtp.gmail.com` | Your provider's SMTP server. |
| `BHR_SMTP_PORT` | email | `465` | `465` uses TLS from the start. Any other port (normally `587`) uses STARTTLS. |
| `BHR_MAIL_FROM` | email | same as `BHR_SMTP_USER` | The From address. |
| `BHR_MAIL_ATTACH_CSV` | email | off | Set to `1` to attach the change list as a CSV. See [Privacy and security](#privacy-and-security). |
| `BHR_ORG_TRACKER_HOME` | optional | the folder `org_tracker.py` is in | Where snapshots, reports and the log are written. |
| `BHR_SECRETS_FILE` | installers, `onboard.py` | `~/.secrets.env` | Which secrets file the scheduled job reads. |
| `BHR_ALLOW_SHRINK` | optional | off | Set to `1` for one run to accept a roster that shrank by more than half. See [When things go wrong](#when-things-go-wrong). |
| `BHR_TRACKER_HOUR`, `BHR_TRACKER_MINUTE` | macOS installer | `9`, `5` | Daily run time for `install_cron.sh`. |
| `BHR_TRACKER_ONCALENDAR` | Linux installer | `*-*-* 09:05:00` | systemd `OnCalendar` schedule for `install_systemd.sh`. |

`BHR_SESSION_COOKIE` also exists, for an [experimental fallback](#experimental-session-cookie-fallback) you shouldn't need.

### Getting a BambooHR API key

1. Log in to BambooHR in your browser.
2. Click your name or photo in the top-right corner and choose **API Keys**.
3. Click **Add New Key**, give it a name you'll recognize later (like "org tracker"), and generate it.
4. Copy the key right away. BambooHR shows it only once.

If you don't see **API Keys** in that menu, your company has turned API access off for your account. Ask your BambooHR admin; there's no workaround on your side.

The key acts as you. It can read whatever your account can read, so treat it like a password. You can revoke it from the same page at any time.

### Getting an app password for email

Most mail providers won't accept your regular password from a script once two-step verification is on. You create a separate app password for this one tool, and you can revoke it later without touching your main login.

| Provider | Server and port | Where to create an app password |
|---|---|---|
| Gmail | `smtp.gmail.com`, `465` | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (needs 2-Step Verification turned on) |
| Fastmail | `smtp.fastmail.com`, `465` | Settings → Privacy & Security → Integrations → New app password, with SMTP access |
| Outlook.com | `smtp-mail.outlook.com`, `587` | account.microsoft.com → Security → Advanced security options → App passwords |
| Microsoft 365 (work) | `smtp.office365.com`, `587` | Depends on your organization; many block SMTP sign-in entirely |

Microsoft has been phasing out password-based SMTP sign-in, so Outlook and Microsoft 365 are the most likely to refuse. Google Workspace accounts managed by an employer often have app passwords disabled too. If yours won't work, send from a personal Gmail or Fastmail account instead; `BHR_MAIL_TO` can still be any address.

### Check that it works

```bash
. ~/.secrets.env
python3 org_tracker.py --check-auth
```

You should see something like this (made-up numbers):

```
subdomain: acme
cookie:    absent
api key:   present
pdftotext: MISSING (brew install poppler)
API directory: OK -- 412 people, 409 with a department
  => API path is sufficient on its own; no chart exports needed.
```

Ignore the `pdftotext` line; it only matters for the experimental fallback. Then test email:

```bash
python3 org_tracker.py --test-email
```

That sends one message with two made-up people in it and saves nothing.

Finally, take your first snapshot:

```bash
python3 org_tracker.py --run
```

The first run saves a baseline and reports nothing, because there's nothing to compare against yet. Change reports start with the second run.

## Scheduling

The installers generate a small `run_daily.sh` in this folder and schedule it once a day. Each morning it reads your secrets file, runs the daily check, and then builds any weekly or monthly roll-up that's due. Your credentials stay in the secrets file and never get copied into the scheduler's configuration, so rotating a key means editing that one file, with no reinstall.

Both installers need `BHR_SUBDOMAIN` set when you run them, and they refuse to install if the secrets file doesn't exist yet.

`run_daily.sh` contains absolute paths to this folder. If you move the folder, run the installer again.

### macOS (launchd)

```bash
BHR_SUBDOMAIN=acme sh install_cron.sh
```

That installs a launchd job named `com.bhr-org-tracker` that runs daily at 09:05. To pick a different time:

```bash
BHR_SUBDOMAIN=acme BHR_TRACKER_HOUR=7 BHR_TRACKER_MINUTE=30 sh install_cron.sh
```

On macOS the job always passes `--email --notify`. Email is skipped quietly if you haven't configured it. Notifications post either way; without terminal-notifier they just aren't clickable.

Useful commands:

```bash
launchctl list | grep bhr-org-tracker                       # is it loaded? (2nd column = last exit code)
launchctl kickstart -p gui/$(id -u)/com.bhr-org-tracker     # run it right now
tail -n 50 tracker.log                                      # what happened
```

### Linux (systemd user timer)

```bash
BHR_SUBDOMAIN=acme sh install_systemd.sh
sh install_systemd.sh status
```

This installs a systemd user timer (`bhr-org-tracker.timer`) that runs daily at 09:05 plus a random delay of up to five minutes. It runs as you and needs no root. To change the time:

```bash
BHR_SUBDOMAIN=acme BHR_TRACKER_ONCALENDAR='*-*-* 07:30:00' sh install_systemd.sh
```

**Turn on linger.** A user timer only fires while you're logged in unless linger is enabled. The installer warns you if it's off. Fix it once with:

```bash
sudo loginctl enable-linger $USER
```

The Linux job passes `--email` but not `--notify`, since notifications are macOS-only. To run it immediately:

```bash
systemctl --user start bhr-org-tracker.service
```

### Plain cron

If your machine has no systemd, run `install_systemd.sh` anyway. It still writes `run_daily.sh`, then prints a crontab line that runs it at the configured time, and you add that line with `crontab -e`. The time comes from `BHR_TRACKER_ONCALENDAR` when it has the simple `*-*-* HH:MM` form; anything else gets 09:05, and the installer says so.

## Commands

Run these from the project folder after loading your settings with `. ~/.secrets.env`.

| Command | What it does |
|---|---|
| `python3 org_tracker.py --run` | Pull the directory, save today's snapshot, compare to the last one, write a report. |
| `python3 org_tracker.py --run --email` | Same, then email the result. If the run fails, emails a failure notice instead. |
| `python3 org_tracker.py --run --notify` | Same, then post a macOS notification. Combine with `--email` as needed. |
| `python3 org_tracker.py --check-auth` | Test your BambooHR credentials. Doesn't save a snapshot. |
| `python3 org_tracker.py --test-email` | Send one sample email and exit. |
| `python3 org_tracker.py --test-notify` | Post one sample notification (macOS) and exit. Its sample page is `reports/changes-test.html`, so today's real report is never replaced. |
| `python3 org_tracker.py --diff-only` | Re-compare the two newest snapshots without calling BambooHR. |
| `python3 org_tracker.py --rollup weekly` | Build this week's roll-up now (Monday to Monday). |
| `python3 org_tracker.py --rollup monthly` | Build last calendar month's roll-up now. |
| `python3 org_tracker.py --rollup auto` | Build whichever roll-ups are due and not yet built. This is what the schedule runs. |
| `python3 org_tracker.py --day YYYY-MM-DD ...` | Treat a different date as today. Works with `--run`, `--rollup`, `--test-email` and `--test-notify`. |
| `python3 org_tracker.py --help` | Full option list. |

`--email` and `--notify` also work with `--rollup`. The remaining options (`--plan`, `--plan-from`, `--parse-pdf`, `--root`, `--levels`, `-v`) belong to the experimental fallback described at the end.

## Output files

Everything goes in the project folder, or in `BHR_ORG_TRACKER_HOME` if you set it.

| Path | Contents |
|---|---|
| `snapshots/snapshot-YYYY-MM-DD.csv` | The full directory on that day: `PersonID`, `Name`, `SupervisorID`, `SupervisorName`, `Department`, `Division`, `Job Title`, `Location`. |
| `reports/changes-YYYY-MM-DD.csv` | That day's changes: `change_type` (`new_hire` or `departed`), `person_id`, `name`, `job_title`, `department`, `division`, `supervisor_name`, `detected_date`. |
| `reports/changes-YYYY-MM-DD.html` | The same changes as a page. Written only when `--notify` is used (the macOS schedule does this); it's what the notification opens. |
| `reports/changes-test.html` | Sample page written by `--test-notify`, with made-up names. Safe to delete. |
| `reports/rollup-weekly-YYYY-MM-DD.csv` / `.html` | Weekly roll-up, named by the Monday it ends on. |
| `reports/rollup-monthly-YYYY-MM.csv` / `.html` | Monthly roll-up, named by the month it covers. |
| `exports/` | Empty unless you use the experimental fallback. |
| `tracker.log` | One line per event, appended on every run. |

Snapshots and reports are `0600` files in `0700` folders, so only your user account can read them. `tracker.log` is created with your normal file permissions; on the API path it holds counts, file names and department names, not people's names. The experimental fallback can also log fragments of org-chart text, which may include names.

People are matched by their BambooHR employee ID, not by name. Someone who changes their name isn't reported as a departure plus a new hire, and two people with the same name stay two people. A departure is reported with the title, department and manager from the last snapshot they appeared in, because once someone is gone from BambooHR you can't look that up anymore.

Skipping days is fine. Each run compares against the most recent earlier snapshot, whenever that was, and running twice on the same day just redoes that day.

## Weekly and monthly roll-ups

You don't schedule these separately. The daily job runs `--rollup auto` after the daily check, and that works out what's due.

- **Weekly:** compares the snapshot from this Monday (or the latest before it) to the one from the Monday before. It's built on Monday, or on the first later day the job runs.
- **Monthly:** covers the previous calendar month, comparing its last snapshot to the last snapshot of the month before. It's built on the first weekday (Monday to Friday) of the new month, or the first run after that. Public holidays aren't taken into account.

Each roll-up shows departures and hires by department, the people who changed, the headcount at the start and end, and the headcount at each snapshot in between.

Once a roll-up file exists, `auto` won't build it again. To rebuild one, delete its file or run `--rollup weekly` / `--rollup monthly` directly.

A few things to expect:

- In the first week or month you won't have a full period of history. The roll-up starts from your earliest snapshot and says so. If only one snapshot falls in the period, the email is titled `insufficient history` instead of claiming nothing changed.
- Someone who joins and leaves inside the same week won't appear in that week's roll-up, because roll-ups compare two snapshots. They'll still be in the daily reports.
- The roll-up step runs even if that morning's daily check failed, so one bad day doesn't cost you a monthly report.

## When things go wrong

The script is built so that a failure never looks like a quiet day.

**A failed run sends a failure email** (when email is configured) with a subject like `BambooHR tracker FAILED — 2026-10-06`, the reason, and what was saved: nothing (the next run tries again); an earlier run's snapshot for that day, which stands; or this run's snapshot, saved before a later step failed. In that last case the next run compares against it and won't report that day's changes again, so run `python3 org_tracker.py --diff-only` the same day to get the report (the weekly roll-up still counts them). On macOS the notification says "Tracker FAILED", and clicking it opens the log. The script exits non-zero.

**An expired, revoked or wrong API key** stops the run before any snapshot is saved. You'll never get a "no changes" email because BambooHR said no.

**A suspiciously small roster is refused.** If BambooHR returns fewer than half as many people as your last snapshot, the script treats it as a broken pull and stops with `roster dropped from N to M (>50%)`. This also trips if your own BambooHR access was narrowed. A refused roster leaves nothing on disk: no snapshot is written, and a snapshot already taken that day is not replaced. If the drop is real, run the daily check once with the override:

```bash
BHR_ALLOW_SHRINK=1 python3 org_tracker.py --run
```

The tracker remembers that you accepted it, in `snapshots/accepted-drops.txt` (mode `0600`), so the weekly and monthly roll-ups that span the drop go through on their own. A roll-up that spans a large drop you never accepted is still refused, and so is one where the roster bounced back after an accepted drop and then fell by more than half again.

**A laptop that's asleep at 09:05** runs the job when it wakes. If it was fully shut down, macOS skips that day; the next run compares against your last snapshot, so nothing is lost, just reported a day late. On Linux, the systemd timer catches up after boot.

**A network that isn't ready yet**, which is common right after a laptop wakes up, is handled by waiting. The script waits up to 120 seconds for BambooHR to be reachable, and makes up to 3 attempts at each request (pausing 2 seconds, then 8). Each request times out after 90 seconds. Certificate errors are not retried: they usually mean a captive portal or a network that intercepts TLS, and waiting won't fix that.

**Departments or job titles missing for a few people** produce a note in the email, not a failure. The report goes out with what BambooHR provided.

## Privacy and security

This tool copies HR data out of BambooHR onto your computer. Before you set it up, check your company's policies on handling employee data. Being able to see the directory in BambooHR doesn't automatically mean you're allowed to store or email it.

What the script does to keep that data contained:

- **It only sees what you see.** Your API key acts as you. If your account can't see someone, or a field like department, neither can this.
- **Credentials only travel in HTTP headers.** The API key is never put in a URL and never written to a log, snapshot or report. The SMTP password is only used to log in.
- **Credentials live in one file.** The secrets file is `0600`, and the scheduled job reads it at run time. The launchd plist and systemd units contain no secrets. The one exception is the `0600` backup `onboard.py` leaves at `~/.secrets.env.bak` when it changes an existing file; delete it after you rotate a key.
- **Snapshots and reports are `0600`** in `0700` folders.
- **Emails name only the people who changed.** The full directory snapshot is never emailed. `BHR_MAIL_ATTACH_CSV=1` adds the change list as a CSV attachment, which is the same people as the email body plus their employee IDs and divisions. It's off by default; think about where your mail ends up before turning it on.
- **Notifications show counts and departments, never names**, since anyone near your screen can read them.

`.gitignore` excludes `snapshots/`, `reports/`, `exports/`, logs, `run_daily.sh` and `*.env` files, so a fork or a pull request won't accidentally include employee data or keys. Keep it that way.

If you leave the company or stop using the tool, see [Uninstalling](#uninstalling). Revoke the API key and delete the snapshots; they're a copy of your company's directory.

## Troubleshooting

Start with `tail -n 50 tracker.log` in the project folder. Most problems show up there in plain words.

| Symptom | Likely cause and fix |
|---|---|
| `API key rejected (401)` | The key was revoked, was typed wrong, or belongs to a different BambooHR site (a sandbox key won't work against your real company). Make a new key and update `BHR_PERSONAL_API_KEY`. |
| `API returned 404 for subdomain` | `BHR_SUBDOMAIN` is wrong, or your account has no API access. Check the address you use to log in to BambooHR. |
| `API returned 403` or `API directory returned no employees` | Your company hides the employee directory from your account or has turned off API access. Only your BambooHR admin can change that. |
| `API directory response is not JSON`, or a message containing `unexpected shape` | Something other than BambooHR's API answered, often a proxy or captive portal, or the subdomain is wrong. Run `--check-auth`. |
| `API roster unavailable: <reason>. Next step: ...` | The BambooHR API call failed and the experimental fallback isn't set up, so the run stopped there. The text after the colon is the actual cause; look it up in this table, fix it, then run `--check-auth`. |
| `No roster source is configured` | `BHR_PERSONAL_API_KEY` isn't set in the environment the run sees. Add it to your secrets file and load the file with `. ~/.secrets.env` before running by hand. |
| `BHR_SUBDOMAIN is not set` | Your settings weren't loaded. Run `. ~/.secrets.env` first, or check that the secrets file sets it. |
| `email not configured` | One of `BHR_SMTP_USER`, `BHR_SMTP_PASSWORD` or `BHR_MAIL_TO` is missing. |
| `SMTP login rejected` | You used your normal password instead of an app password, or your provider blocks SMTP sign-in. See [Getting an app password](#getting-an-app-password-for-email). |
| `SMTP send failed` mentioning SSL or a timeout | Port and encryption don't match. Use `465`, or `587` for providers that require STARTTLS. |
| No email at all, and no failure email | The job didn't run. On macOS, check `launchctl list \| grep bhr-org-tracker`. On Linux, run `sh install_systemd.sh status` and check linger. |
| `roster dropped from N to M (>50%)` | See the small-roster entry under [When things go wrong](#when-things-go-wrong). |
| Notification says "Notifications are not allowed" or never appears, or `tracker.log` says `terminal-notifier lacks notification permission` | macOS hasn't registered terminal-notifier yet. Run `open "$(brew --prefix)/opt/terminal-notifier/terminal-notifier.app"` once, then allow it in System Settings → Notifications. |
| Clicking the notification does nothing | terminal-notifier isn't installed, so the script fell back to a plain notification that can't be clicked. Run `brew install terminal-notifier`. |
| `Operation not permitted` in the log, or the job exits right away (macOS) | The project folder is in Desktop, Documents, Downloads or an iCloud-synced folder, which macOS blocks for scheduled jobs. Move it (for example to `~/bhr-org-tracker`) and run the installer again. |
| `Permission denied` running `org_tracker.py` from the schedule (macOS) | The file lost its executable bit (some zip tools drop it). Run `chmod +x org_tracker.py`. |
| Linux timer only runs while you're logged in | Linger is off. Run `sudo loginctl enable-linger $USER`. |
| `secrets file missing` in the log | The job can't find the secrets file it was installed with. Restore it, or rerun the installer with the right `BHR_SECRETS_FILE`. |

## Running the tests

```bash
python3 test_tracker.py
python3 test_onboard.py
```

Neither suite uses the network or real credentials, and neither touches your snapshots. On a fresh clone every check should pass. Three groups in `test_tracker.py` need private sample data from a real company and print `SKIP` instead, and a fourth prints `SKIP` on systems without `/bin/dash`. The exact counts vary a little by Python version. Both scripts end with an `N passed, M failed` line and exit non-zero if anything failed.

## Uninstalling

1. Remove the schedule. This leaves the runner and your secrets file in place.

   ```bash
   sh install_cron.sh remove        # macOS
   sh install_systemd.sh remove     # Linux
   ```

   For plain cron, delete the line with `crontab -e`.

2. Delete the `BHR_*` lines from `~/.secrets.env` (or the whole file if nothing else uses it), and delete `~/.secrets.env.bak` if `onboard.py` left one.
3. Revoke the API key in BambooHR (your name → **API Keys**) and the app password at your mail provider.
4. Delete the project folder. It holds your snapshots and reports, which are copies of your company's employee directory.

## Experimental: session-cookie fallback

There's a second way to get the directory, for when API keys aren't available: a logged-in browser session cookie (`BHR_SESSION_COOKIE`), BambooHR's org-chart export pages, and `pdftotext` from poppler to read the exported PDFs. That's what `--plan`, `--plan-from`, `--parse-pdf`, `--root`, `--levels` and `-v` are for. If the API call fails and a cookie is set, `--run --root <employee id>` falls back to it.

Treat it as unsupported. Session cookies expire, so it can't run unattended for long. The PDF parser also depends on a hardcoded list of division names (the `DIVISIONS` constant near the top of `org_tracker.py`) that matches one particular company's BambooHR setup. At any other company it won't parse correctly until you edit that list to match your own division names. Use the API key path.
