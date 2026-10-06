#!/usr/bin/env python3
"""onboard.py -- guided setup for bhr-org-tracker:  python3 onboard.py [--dry-run] [--secrets-file PATH]

Run it again any time: saved values become defaults; lines it does not manage are left alone. Secrets are
typed with getpass, never shown, and reach child processes through their environment only."""

import argparse, getpass, os, re, shlex, shutil, subprocess, sys, tempfile, textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
TRACKER = os.path.join(HERE, "org_tracker.py")
INSTALLERS = {"darwin": os.path.join(HERE, "install_cron.sh"),
              "linux": os.path.join(HERE, "install_systemd.sh")}
STEPS, WIDTH, PLATFORM = 9, 76, sys.platform

# The keys this script writes; every other line in the settings file is copied through.
MANAGED_KEYS = ("BHR_SUBDOMAIN", "BHR_PERSONAL_API_KEY", "BHR_SMTP_HOST", "BHR_SMTP_PORT",
                "BHR_SMTP_USER", "BHR_SMTP_PASSWORD", "BHR_MAIL_TO")
EMAIL_NEEDS = ("BHR_SMTP_USER", "BHR_SMTP_PASSWORD", "BHR_MAIL_TO")   # host and port have defaults
HEADER = "# bhr-org-tracker settings -- written by onboard.py; run it again to change them\n"
UNSAFE_PATH_CHARS = '"$`\n'   # would break out of the "..." the installers paste paths into
EMAIL_RE = r"[^@\s,]+@[^@\s,]+\.[^@\s,]+"

# (name, SMTP host, port, where to get an app password)
SMTP_PRESETS = [
    ("Gmail", "smtp.gmail.com", "465", "turn on 2-Step Verification, then visit myaccount.google.com/apppasswords"),
    ("Fastmail", "smtp.fastmail.com", "465", "Settings > Privacy & Security > Integrations"),
    ("Outlook.com", "smtp-mail.outlook.com", "587", "account.microsoft.com > Security > App passwords"),
    ("Microsoft 365", "smtp.office365.com", "587", "your IT department decides; ask them for one"),
]

RESPONSIBLE_USE = (
    "Before you start: this tool reads your company's employee directory through your own BambooHR account "
    "-- names, job titles, departments and managers -- and keeps a daily copy to spot who joined and who "
    "left. Check that your company's policies allow that. The copies and the settings file stay on this "
    "computer, readable only by you. Emails name people who joined or left; send them only to a mailbox you trust.")


# -- the settings file -------------------------------------------------------

_ASSIGN = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.S)


def is_expression(text):
    """Whether sh would expand a $ or ` in this text (single quotes and backslash protect)."""
    parts = re.findall(r"""'[^']*(?:'|\Z)|"(?:\\.|[^"\\])*(?:"|\Z)|\\.|[^'"\\]+""", text, re.S)
    return any(p[0] not in "'\\" and ("$" in p or "`" in p) for p in parts)


def parse_settings(text):
    """Entries {raw, key, value, expr}, one per line, whose raws join back into text. Only managed keys
    get a key. A value sh would expand, or a quote left open onto the next line, counts as an expression."""
    out = []
    for raw in text.splitlines(keepends=True):
        m, entry = _ASSIGN.match(raw), {"raw": raw, "key": None, "value": None, "expr": False}
        if m and m.group(1) in MANAGED_KEYS:
            rest = m.group(2)
            lex = shlex.shlex(rest, posix=True)   # sh's first word, with '#' literal inside it
            lex.whitespace_split, lex.commenters = True, ""
            try:
                value = "" if rest[:1] in ("", " ", "\t", "\n") else lex.get_token() or ""
            except ValueError:                    # an open quote
                value = None
            expr = value is None or is_expression(rest)
            entry.update(key=m.group(1), expr=expr, value=None if expr else value)
        out.append(entry)
    return out


def merge_settings(text, updates):
    """Foreign and expression lines stay verbatim; other managed keys appear once, where first seen."""
    entries = parse_settings(text)
    exprs = {e["key"] for e in entries if e["expr"]}
    last = {e["key"]: e["raw"] for e in entries if e["key"]}
    out, seen = [], set(exprs)
    for e in entries:
        key = e["key"]
        if key is None or key in exprs:
            out.append(e["raw"])
        elif key not in seen:             # later duplicates are dropped
            seen.add(key)
            line = f"export {key}={shlex.quote(updates[key])}\n" if key in updates else last[key]
            out.append(line if line.endswith("\n") else line + "\n")
    body = "".join(out)
    new = [k for k in MANAGED_KEYS if k in updates and k not in seen]
    if new:
        body += "" if not body or body.endswith("\n") else "\n"
        body += "" if HEADER in body else ("\n" if body.strip() else "") + HEADER
        body += "".join(f"export {k}={shlex.quote(updates[k])}\n" for k in new)
    return body


def write_settings(path, text):
    """Save at 0600 through any symlink, first copying an existing file to <path>.bak; each write
    is a 0600 temp file renamed over its target. Returns the backup's path, if one was made."""
    real = os.path.realpath(path)
    os.makedirs(os.path.dirname(real), mode=0o700, exist_ok=True)
    writes = [(real, text.encode("utf-8", "surrogateescape"))]
    if os.path.exists(real):
        with open(real, "rb") as fh:
            writes.insert(0, (os.path.abspath(path) + ".bak", fh.read()))
    for dest, data in writes:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".onboard-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    return writes[0][0] if len(writes) == 2 else None


# -- terminal I/O (tests replace input, getpass and run_child) ---------------

def run_child(argv, env=None, capture=False):
    """(exit code, captured output). Secrets go in env, never in argv."""
    pipe = subprocess.PIPE if capture else None
    p = subprocess.run(argv, env=env, text=True, stdout=pipe, stderr=pipe and subprocess.STDOUT)
    return p.returncode, p.stdout or ""


def say(text, indent=2):
    print(textwrap.fill(text, WIDTH, initial_indent=" " * indent, subsequent_indent=" " * indent,
                        break_long_words=False, break_on_hyphens=False))


def mask(value):
    return ("****" + value[-4:] if len(value) >= 12 else "****") if value else "(not set)"


def ask(prompt, default=None, check=None, secret=False):
    """Ask until check(answer) -> (value, error) accepts. Secrets: no echo, masked default."""
    hint = (f" [Enter keeps {mask(default)}]" if secret else f" [{default}]") if default else ""
    while True:
        raw = (getpass.getpass if secret else input)(f"  {prompt}{hint}: ").strip()
        if secret and raw:
            print(f"    got it ({mask(raw)})")
        raw = raw or default or ""
        value, err = check(raw) if check and raw else (raw, None if raw else "(an answer is needed)")
        if not err:
            return value
        print(f"    {err}")


def ask_yes(prompt, default):
    while True:
        raw = input(f"  {prompt} {'[Y/n]' if default else '[y/N]'}: ").strip().lower()
        if raw in ("", "y", "yes", "n", "no"):
            return raw.startswith("y") if raw else default
        print("    (please answer y or n)")


def valid(pattern, error, fix=str):
    """A check for ask(): the answer must match pattern; fix() tidies it."""
    return lambda raw: (fix(raw), None) if re.fullmatch(pattern, raw) else (None, error)


# -- the walkthrough ---------------------------------------------------------

class Setup:
    def __init__(self, dry_run, secrets_file):
        self.dry, self.platform, self.T = dry_run, PLATFORM, None
        self.path = os.path.abspath(os.path.expanduser(
            secrets_file or os.environ.get("BHR_SECRETS_FILE") or "~/.secrets.env"))
        self.text, self.plain, self.exprs, self.values = "", {}, set(), {}
        self.verified = self.saved = False
        self.backup = self.stop = None
        self.email, self.schedule = "not set up", "not scheduled"

    def current(self, key):
        return self.values.get(key) or self.plain.get(key) or os.environ.get(key) or None

    def kept(self, *keys):   # a line set by a shell expression is never asked about or rewritten
        found = [k for k in keys if k in self.exprs]
        if found:
            say(f"{', '.join(found)}: set by a shell expression in your settings file -- kept as written.", 4)
        return bool(found)

    def sourced(self, *args):   # loads the settings in a subshell, so secrets don't linger
        return f"( . {shlex.quote(self.path)} && python3 {shlex.quote(TRACKER)} {' '.join(args)} )"

    def step(self, n, title):
        print(f"\n== Step {n} of {STEPS}: {title} ".ljust(WIDTH, "=") + "\n")

    def run(self):
        say("bhr-org-tracker setup. Press Ctrl-C at any question to stop; nothing is saved until step 6."
            + (" DRY RUN: nothing will be saved, installed or run." if self.dry else ""), 0)
        rc = self.preflight()
        if rc is not None:
            return rc
        for part in (self.bamboohr, self.verify, self.email_step, self.notifications):
            part()
        if self.save():
            for part in (self.first_run, self.schedule_daily, self.summary):
                part()
        return 0

    def preflight(self):
        self.step(1, "Check this computer")
        say(f"Python {sys.version.split()[0]}, {self.platform}"
            + ("" if self.platform in INSTALLERS else " (no daily scheduling here)"))
        problem = None
        if sys.version_info < (3, 9):
            problem = "This needs Python 3.9 or newer."
        elif not os.path.isfile(TRACKER):
            problem = f"org_tracker.py isn't in {HERE}; keep onboard.py in the same folder as it."
        elif any(c in self.path for c in UNSAFE_PATH_CHARS) or os.path.isdir(self.path):
            problem = (f"Can't use {self.path!r} as the settings file: it is a folder, or contains \" $ ` "
                       "or a line break. Pick another with --secrets-file.")
        if problem:
            say(problem)
            return 1
        sys.dont_write_bytecode = True    # a dry run leaves no __pycache__ behind
        sys.path.insert(0, HERE)
        import org_tracker as T
        T.HTTP_TIMEOUT, T.HTTP_RETRIES, T.log = 30, 2, lambda msg: print("    " + msg)
        self.T = T
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
                self.text = fh.read()
        entries = parse_settings(self.text)   # the last definition wins, as in sh
        self.plain = {e["key"]: e["value"] for e in entries if e["key"] and not e["expr"]}
        self.exprs = {e["key"] for e in entries if e["expr"]}
        say(f"Settings file: {self.path} -- "
            + ("saved values are offered as defaults." if self.text else "will be created."))
        print()
        say(RESPONSIBLE_USE)
        if not ask_yes("Continue?", True):
            say("OK -- nothing was changed.")
            return 0

    def bamboohr(self):
        self.step(2, "Connect to your BambooHR account")
        self.ask_account()

    def ask_account(self):
        if not self.kept("BHR_SUBDOMAIN"):
            say("Your BambooHR address looks like https://NAME.bamboohr.com. Type NAME or paste the address.")
            check = valid(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                          "That isn't a BambooHR name -- it's the part before .bamboohr.com.")
            self.values["BHR_SUBDOMAIN"] = ask(   # org_tracker's own normalisation, lower-cased
                "Company name in your BambooHR address", self.current("BHR_SUBDOMAIN"),
                lambda raw: check(self.T.Session(raw, None, None).subdomain.lower()))
        if not self.kept("BHR_PERSONAL_API_KEY"):
            say("Next, an API key. Sign in to BambooHR, click your name or photo (top right), choose 'API "
                "Keys', then 'Add New Key', and copy it -- it is shown only once. No 'API Keys' in that "
                "menu? Your company has turned them off for you; ask your BambooHR administrator.")
            self.values["BHR_PERSONAL_API_KEY"] = ask("Paste your API key (it won't show)",
                                                      self.current("BHR_PERSONAL_API_KEY"), secret=True)

    def verify(self):
        self.step(3, "Check the connection")
        while not self.kept("BHR_SUBDOMAIN", "BHR_PERSONAL_API_KEY"):
            sub, key = self.values["BHR_SUBDOMAIN"], self.values["BHR_PERSONAL_API_KEY"]
            say(f"Asking BambooHR for the {sub} employee directory ...")
            try:
                people = self.T.fetch_roster_api(self.T.Session(sub, None, key))
            except self.T.TrackerError as exc:
                say(f"That didn't work: {exc}", 4)
            else:   # counts only -- never a name
                depts = sum(1 for p in people if p.get("Department"))
                self.verified = True
                return say(f"Connected. Your account can see {len(people)} people; {depts} have a department.", 4)
            if not ask_yes("Re-enter the company name and key and try again? (No: carry on)", True):
                return
            self.ask_account()
        say(f"Not checked here. Check it later with: {self.sourced('--check-auth')}")

    def email_step(self):
        self.step(4, "Daily email (optional)")
        say("The tracker can email you who joined and who left, daily, weekly and monthly. It sends "
            "from your own email account using an 'app password', so your real password is never stored.")
        if self.kept(*MANAGED_KEYS[2:]):   # those lines are never rewritten, so the rest is finished by hand
            missing = [k for k in EMAIL_NEEDS if k not in self.exprs and not self.plain.get(k)]
            self.email = f"NOT set up -- add {', '.join(missing)} to {self.path}" if missing else "kept as written"
            return say(f"Email is {self.email}.", 4) if missing else None
        have = all(self.plain.get(k) for k in EMAIL_NEEDS)
        self.email = "kept as it was" if have else self.email
        if not ask_yes(f"Email goes to {self.plain['BHR_MAIL_TO']}. Change that?" if have
                       else "Set up the daily email?", not have):
            return
        cfg = self.ask_email()
        while ask_yes("Send a test email now? It uses made-up names.", True) and not self.send_test(cfg):
            if ask_yes("Change the email settings before trying again?", True):
                cfg = self.ask_email()
        self.values.update(cfg)
        self.email = "to " + cfg["BHR_MAIL_TO"]

    def ask_email(self):
        now, cfg = self.current("BHR_SMTP_HOST"), {}
        print("  Which email service sends it?\n"
              + "".join(f"    [{i}] {p[0]}\n" for i, p in enumerate(SMTP_PRESETS, 1))
              + "    [5] another one (you'll need its SMTP server and port)")
        c = ask("Choose", next((str(i) for i, p in enumerate(SMTP_PRESETS, 1) if p[1] == now),
                               "5" if now else "1"), valid("[1-5]", "(pick a number above)"))
        if c == "5":
            cfg["BHR_SMTP_HOST"] = ask("Outgoing (SMTP) server, e.g. smtp.example.com", now,
                                       valid(r"[\w.-]+\.[A-Za-z]{2,}", "That isn't a server name.", str.lower))
            cfg["BHR_SMTP_PORT"] = ask("Port (usually 465 or 587)", self.current("BHR_SMTP_PORT") or "465",
                                       valid(r"[1-9]\d{0,4}", "A port is a number, usually 465 or 587."))
        else:
            name, cfg["BHR_SMTP_HOST"], cfg["BHR_SMTP_PORT"], how = SMTP_PRESETS[int(c) - 1]
            say(f"{name} needs an app password: {how}.")
        cfg["BHR_SMTP_USER"] = ask("Email address to send from", self.current("BHR_SMTP_USER"),
                                   valid(EMAIL_RE, "That doesn't look like an email address."))
        old = self.current("BHR_SMTP_PASSWORD")
        pw = ask("App password for that account (it won't show)", old, secret=True)
        gmail = pw != old and cfg["BHR_SMTP_HOST"] == "smtp.gmail.com"   # Google shows groups of four
        cfg["BHR_SMTP_PASSWORD"] = pw.replace(" ", "") if gmail else pw
        cfg["BHR_MAIL_TO"] = ask("Send the reports to (commas between several)",
                                 self.current("BHR_MAIL_TO") or cfg["BHR_SMTP_USER"],
                                 valid(rf"{EMAIL_RE}(\s*,\s*{EMAIL_RE})*", "Those aren't all email addresses.",
                                       lambda r: ",".join(a.strip() for a in r.split(","))))
        return cfg

    def send_test(self, cfg):
        """org_tracker's own --test-email: settings in its environment, a throwaway data folder."""
        if self.dry:
            return say(f"[dry run] would send a test email to {cfg['BHR_MAIL_TO']}; nothing was sent.") or True
        with tempfile.TemporaryDirectory(prefix="bhr-onboard-") as scratch:
            rc, out = run_child([sys.executable, TRACKER, "--test-email"],
                                dict(os.environ, **cfg, BHR_ORG_TRACKER_HOME=scratch), capture=True)
        print("\n".join("    " + line for line in out.splitlines()))
        if rc == 0:
            say("Sent. Check that inbox (and its spam folder) in a minute or two.", 4)
        return rc == 0

    def notifications(self):
        self.step(5, "Desktop notifications (optional)")
        if self.platform != "darwin" or shutil.which("terminal-notifier"):
            return say("Nothing to do: notifications are macOS-only, or terminal-notifier is installed.")
        say("The daily run posts a notification either way; with the free terminal-notifier app, clicking it "
            "opens the report. To add it:  brew install terminal-notifier")
        if shutil.which("brew") and ask_yes("Install it now with Homebrew?", False):
            if self.dry:
                return say("[dry run] would run: brew install terminal-notifier")
            run_child(["brew", "install", "terminal-notifier"])

    def save(self):
        self.step(6, "Save your settings")
        updates = {k: v for k, v in self.values.items() if v != self.plain.get(k)}
        new_text, exists = merge_settings(self.text, updates), os.path.exists(self.path)
        if exists and new_text == self.text and not os.stat(self.path).st_mode & 0o077:
            self.saved = True
            return say(f"Nothing to change in {self.path}.") or True
        if self.dry:
            return say(f"[dry run] would write {self.path} (mode 0600"
                       + (", after backing up the current file)." if exists else ").")) or True
        if not ask_yes(f"Save to {self.path}?", True):
            return say("Not saved -- nothing was changed.")
        try:
            self.backup = write_settings(self.path, new_text)
        except OSError as exc:
            return say(f"Couldn't save {self.path}: {exc.strerror or exc}. Nothing was changed.")
        self.saved = True
        say(f"Saved {self.path} (readable only by you).")
        if self.backup:
            say(f"The previous version is in {self.backup}. It holds secrets too -- delete it once "
                "you're happy with the new settings:")
            print(f"      rm {shlex.quote(self.backup)}")
        return True

    def first_run(self):
        self.step(7, "Take the first snapshot (optional)")
        say("The first run saves a copy of today's directory; reports of changes start with the next run.")
        if not ask_yes("Run it now?", self.verified):
            return
        if self.dry:
            return print(f"  [dry run] would run:\n      {self.sourced('--run')}")
        # sourced, as the daily runner does, so the key travels in the environment
        rc, _ = run_child(["/bin/sh", "-c", '. "$1" && exec "$2" "$3" --run', "sh", self.path, sys.executable, TRACKER])
        if rc:
            say(f"That run failed; the message above says why. Try again with: {self.sourced('--run')}")

    def schedule_daily(self):
        self.step(8, "Run it every day (optional)")
        installer = INSTALLERS.get(self.platform)
        if not installer:
            return say(f"Scheduling isn't supported here. Run it by hand: {self.sourced('--run')}")
        say("The tracker can run once a day by itself, and email you the changes if email is on.")
        if not ask_yes("Set up the daily run?", True):
            return
        hhmm = ask("What time each day? (24-hour)", "09:05",
                   valid(r"([01]?\d|2[0-3])(:[0-5]\d)?", "Use a 24-hour time like 09:05 or 17:30.",
                         lambda r: "%02d:%02d" % tuple(map(int, (r + ":0").split(":")[:2]))))
        home = os.path.abspath(os.environ.get("BHR_ORG_TRACKER_HOME") or HERE)
        sub = self.current("BHR_SUBDOMAIN")
        if any(c in HERE + home for c in UNSAFE_PATH_CHARS) or not sub:
            return say(f"Can't run the installer: {HERE} or {home} contains \" $ ` or a line break, or the "
                       f"company name is a shell expression. Fix that, then: BHR_SUBDOMAIN=NAME sh {installer}")
        # Only what the installers read -- no API key, cookie or password.
        env = {k: v for k, v in os.environ.items() if not k.startswith("BHR_")}
        env.update(BHR_SUBDOMAIN=sub, BHR_SECRETS_FILE=self.path, BHR_ORG_TRACKER_HOME=home)
        hour, minute = map(int, hhmm.split(":"))   # plain integers: install_cron.sh reads "09" as octal
        env.update({"BHR_TRACKER_HOUR": str(hour), "BHR_TRACKER_MINUTE": str(minute)} if self.platform == "darwin"
                   else {"BHR_TRACKER_ONCALENDAR": f"*-*-* {hhmm}:00"})
        if self.dry:
            self.schedule = f"would be set up for {hhmm} (dry run)"
            return print(f"  [dry run] would run, with {', '.join(k for k in env if k.startswith('BHR_'))} set:"
                         f"\n      sh {shlex.quote(installer)}")
        rc, out = run_child(["/bin/sh", installer], env, capture=True)
        print("\n".join("    " + line for line in out.splitlines()))
        if rc:
            return say("The scheduler setup failed; the lines above say why. Your settings are saved.")
        if self.platform == "linux" and not shutil.which("systemctl"):   # the installer printed a cron line
            self.schedule = "NOT scheduled yet -- add the crontab line shown in step 8 with 'crontab -e'"
            return say("Nothing runs until you add that line with 'crontab -e'. To stop it later, delete it there.")
        self.schedule, self.stop = f"set up for {hhmm}", f"sh {shlex.quote(installer)} remove"
        print(f"  To stop it later:\n      {self.stop}")

    def summary(self):
        self.step(9, "Done (dry run -- nothing was saved or run)" if self.dry else "Done")
        sub = self.current("BHR_SUBDOMAIN") or "(shell expression)"
        for label, text in [
                ("Settings file", self.path + (f" (delete the backup {self.backup})" if self.backup else "")),
                ("BambooHR", f"{sub}.bamboohr.com, connection " + ("checked" if self.verified else "NOT confirmed")),
                ("Email", self.email), ("Daily run", self.schedule)]:
            print(f"  {label + ':':16}{text}")
        print("\n  Commands for later (copy and paste into a terminal):")
        cmds = [("Check the connection", self.sourced("--check-auth")),
                ("Send a test email", self.sourced("--test-email")), ("Run it now", self.sourced("--run")),
                ("Stop the daily run", self.stop),
                ("Change a setting", f"python3 {shlex.quote(os.path.join(HERE, 'onboard.py'))}")]
        for label, cmd in cmds:
            if cmd:
                print(f"    {label}:\n      {cmd}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="onboard.py", description="Guided setup for bhr-org-tracker.")
    ap.add_argument("--dry-run", action="store_true", help="ask and check everything; save and run nothing")
    ap.add_argument("--secrets-file", metavar="PATH", help="default: $BHR_SECRETS_FILE, else ~/.secrets.env")
    args = ap.parse_args(argv)
    if not sys.stdin.isatty():
        sys.stderr.write("onboard.py asks questions, so run it in a terminal window:  python3 onboard.py\n")
        return 2
    setup = Setup(args.dry_run, args.secrets_file)
    try:
        return setup.run()
    except (KeyboardInterrupt, EOFError):
        print()
        say(f"Stopped. Settings were already saved to {setup.path}; nothing after that changed."
            if setup.saved and not setup.dry else "Stopped -- nothing saved.", 0)
        return 130


if __name__ == "__main__":
    sys.exit(main())
