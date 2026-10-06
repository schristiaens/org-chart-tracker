#!/usr/bin/env python3
"""Offline verification for org_tracker.py. No credentials, no network.

Run: python3 test_tracker.py
"""
import contextlib, csv, io, itertools, os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import org_tracker as T

# Real-company expectations (paths, headcounts, employee names) live in a
# gitignored fixture so this suite carries no PII. Absent fixture -> those
# checks skip and everything else still runs.
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "local_expectations.json")
LOCAL = {}
if os.path.exists(FIXTURE):
    import json as _json
    LOCAL = _json.load(open(FIXTURE))
REAL = os.path.expanduser(LOCAL.get("roster_dir", ""))
PDF = os.path.expanduser(LOCAL.get("pdf_path", ""))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def people(rows):
    return [{"PersonID": p, "Name": n, "SupervisorID": s, "Department": "",
             "Division": "", "Job Title": "", "Location": ""} for p, n, s in rows]


# ---------------------------------------------------------------- planner
def brute_force_min(forest, k):
    """Exhaustive smallest cover, for trees small enough to enumerate."""
    ids = list(forest.by_id)
    for size in range(1, len(ids) + 1):
        for combo in itertools.combinations(ids, size):
            covered = set()
            for r in combo:
                covered |= forest.descendants(r, k)
            if covered == set(ids):
                return size
    return len(ids)


print("\n[planner]")
# a chain: 1->2->...->9, k=3 needs ceil(9/3)=3 exports
chain = people([(str(i), f"P{i}", str(i - 1) if i > 1 else "") for i in range(1, 10)])
f = T.Forest(chain)
plan = T.plan_cover(f, 3)
check("chain of 9 at k=3 -> 3 exports", len(plan) == 3, f"got {len(plan)}")

# the case top-down greedy gets wrong: one L3 manager with 5 reports, k=3
wide = people([("1", "root", ""), ("2", "mid", "1"), ("3", "mgr", "2")]
              + [(str(10 + i), f"kid{i}", "3") for i in range(5)])
f = T.Forest(wide)
plan = T.plan_cover(f, 3)
check("wide-fanout tree at k=3 beats naive top-down", len(plan) == 2, f"got {len(plan)}")

for label, roster, k in [
    ("chain9/k3", chain, 3), ("wide/k3", wide, 3), ("chain9/k2", chain, 2),
    ("wide/k2", wide, 2), ("chain9/k4", chain, 4),
]:
    f = T.Forest(roster)
    got, opt = len(T.plan_cover(f, k)), brute_force_min(f, k)
    check(f"minimal vs brute force: {label}", got == opt, f"planner {got}, optimal {opt}")

# forest: three disconnected components must each get a root
forest_roster = people([("1", "a", ""), ("2", "b", "1"), ("3", "c", ""), ("4", "d", "")])
f = T.Forest(forest_roster)
check("forest: every component covered", len(T.plan_cover(f, 6)) == 3,
      f"got {len(T.plan_cover(f, 6))}")

# monotonicity: more levels per export never needs more exports
f = T.Forest(chain)
counts = [len(T.plan_cover(f, k)) for k in (1, 2, 3, 4, 6)]
check("monotonic in levels", all(a >= b for a, b in zip(counts, counts[1:])), str(counts))

# supervisor cycle must raise, not hang
cyc = people([("1", "a", "2"), ("2", "b", "1")])
try:
    T.Forest(cyc); check("supervisor cycle detected", False, "no raise")
except T.TrackerError:
    check("supervisor cycle detected", True)

# self-supervision is not a cycle
selfsup = people([("1", "a", "1"), ("2", "b", "1")])
try:
    check("self-supervision tolerated", len(T.plan_cover(T.Forest(selfsup), 6)) == 1)
except T.TrackerError as e:
    check("self-supervision tolerated", False, str(e))

print("\n[planner on real roster]")
ROSTER = os.path.join(REAL, LOCAL.get("roster_csv", "")) if REAL else ""
if ROSTER and os.path.exists(ROSTER):
    with open(ROSTER, "rb") as fh:
        real = T.roster_from_csv_bytes(fh.read())
    f = T.Forest(real)
    plan = T.plan_cover(f, 6)
    covered = set()
    for r in plan:
        covered |= f.descendants(r, 6)
    check("real roster: expected headcount", len(real) == LOCAL["roster_people"], str(len(real)))
    check("real roster: expected components",
          len(f.roots) == LOCAL["roster_components"], str(len(f.roots)))
    check("real roster: expected export count",
          len(plan) == LOCAL["roster_exports"], str(len(plan)))
    check("real roster: full coverage", len(covered) == len(real), f"{len(covered)}/{len(real)}")
else:
    print("  SKIP  real-roster checks (no local fixture)")

# ---------------------------------------------------------------- parser
print("\n[parser]")
if LOCAL and PDF and os.path.exists(PDF) and T.pdftotext_available():
    cards, repairs = T.parse_org_pdf(PDF)
    exp = LOCAL["expected_cards"]
    check(f"real PDF: {exp} cards", len(cards) == exp, str(len(cards)))
    byname = {c["name"]: c for c in cards}
    for person, dept in LOCAL["merged_segment_recoveries"].items():
        check("merged segment recovered", byname.get(person, {}).get("dept") == dept, person)
    check("render fragment dropped", LOCAL["dropped_fragment"] not in byname)
    check("every repair is logged",
          len(repairs) == LOCAL["expected_repairs"], f"{len(repairs)} repairs")
    check("Vendor Personnel stays a job title",
          all(c["div"] in T.DIVISIONS for c in cards)
          and any(c["title"] == "Vendor Personnel" for c in cards))
    check("divisions capped at 6", len({c["div"] for c in cards}) == 6)
    try:
        T.parse_org_pdf(PDF, expected=2000)
        check("partial-parse guard raises", False, "no raise at expected=2000")
    except T.TrackerError:
        check("partial-parse guard raises", True)
else:
    print("  SKIP  real-PDF checks (no fixtures/local_expectations.json)")

# ---------------------------------------------------------------- merge
print("\n[attribute merge]")
roster = people([("1", "Ann Lee", ""), ("2", "Ann Lee", "1"), ("3", "Bo Ray", "1")])
cards = [{"name": "Ann Lee", "title": "Eng", "dept": "Tech", "div": "Tech", "reports": None},
         {"name": "Bo Ray", "title": "PM", "dept": "Product", "div": "Product", "reports": None}]
filled = T.merge_attributes(roster, cards)
amb = [p for p in roster if p["Name"] == "Ann Lee"]
check("duplicate name left empty, never guessed",
      all(not p["Department"] for p in amb), str([p["Department"] for p in amb]))
check("unique name filled", roster[2]["Department"] == "Product")
# scoping disambiguates the duplicate
filled = T.merge_attributes(roster, cards, scope_ids={"2", "3"})
check("subtree scoping resolves duplicate", roster[1]["Department"] == "Tech")

# ---------------------------------------------------------------- diff
print("\n[diff]")
tmp = tempfile.mkdtemp(prefix="tracker-test-")
os.environ["BHR_ORG_TRACKER_HOME"] = tmp
T.ensure_dirs()
d1, d2 = LOCAL.get("diff_pair", ("", ""))
if REAL and d1 and os.path.exists(f"{REAL}/snapshot-{d1}.csv"):
    for day in (d1, d2):
        shutil.copy(f"{REAL}/snapshot-{day}.csv", T.snapshot_path(day))
    old = T.read_snapshot(T.snapshot_path(d1))
    new = T.read_snapshot(T.snapshot_path(d2))
    rows = T.diff_snapshots(old, new, d2)
    dep = [r for r in rows if r["change_type"] == "departed"]
    hire = [r for r in rows if r["change_type"] == "new_hire"]
    check("real pair reproduces expected departures",
          len(dep) == LOCAL["diff_departures"], str(len(dep)))
    check("real pair reproduces expected hires",
          len(hire) == LOCAL["diff_hires"], str(len(hire)))
    check("departures carry supervisor from prior snapshot",
          sum(1 for r in dep if r["supervisor_name"]) > len(dep) * 0.9,
          str(sum(1 for r in dep if r["supervisor_name"])))
else:
    print("  SKIP  real-pair diff checks (no local fixture)")
    d1, d2 = "2026-01-01", "2026-01-02"
    synth_old = people([(str(i), f"P{i}", "1" if i > 1 else "") for i in range(1, 60)])
    synth_new = [r for r in synth_old if r["PersonID"] not in ("7", "9")]
    synth_new.append({**synth_old[0], "PersonID": "999", "Name": "New P"})
    T.write_snapshot(synth_old, d1)
    T.write_snapshot(synth_new, d2)
    old = T.read_snapshot(T.snapshot_path(d1))
    new = T.read_snapshot(T.snapshot_path(d2))
    rows = T.diff_snapshots(old, new, d2)
    dep = [r for r in rows if r["change_type"] == "departed"]
    hire = [r for r in rows if r["change_type"] == "new_hire"]
    check("synthetic pair diffs correctly", len(dep) == 2 and len(hire) == 1,
          f"{len(dep)} dep / {len(hire)} hire")

# id-keyed, not name-keyed: a rename is not a hire+departure
a = people([("1", "Jane Smith", "")])
b = people([("1", "Jane Doe", "")])
check("rename is not churn", T.diff_snapshots(a, b, "2026-01-01") == [])
# same name, different ids
c = people([("1", "Sam Roe", ""), ("2", "Sam Roe", "")])
d = people([("1", "Sam Roe", "")])
r = T.diff_snapshots(c, d, "2026-01-01")
check("same-name distinct ids handled", len(r) == 1 and r[0]["person_id"] == "2")

# shrink guard
try:
    T.diff_snapshots(new, new[:2], d2)
    check("shrink guard blocks truncated pull", False, "no raise")
except T.TrackerError:
    check("shrink guard blocks truncated pull", True)
check("shrink guard overridable",
      len(T.diff_snapshots(new, new[:2], d2, allow_shrink=True)) == len(new) - 2)
# baseline: prior_snapshot ignores same/later days
check("prior snapshot resolution",
      os.path.basename(T.prior_snapshot(d2) or "") == f"snapshot-{d1}.csv",
      str(T.prior_snapshot(d2)))
check("no prior before first snapshot", T.prior_snapshot("1990-01-01") is None)

# ---------------------------------------------------------------- writes
print("\n[persistence]")
p = T.write_snapshot(new, "2026-12-31")
check("snapshot written 0600", oct(os.stat(p).st_mode)[-3:] == "600", oct(os.stat(p).st_mode))
check("snapshot round-trips", len(T.read_snapshot(p)) == len(new))
with open(p, newline="", encoding="utf-8") as fh:
    hdr = next(csv.reader(fh))
check("snapshot carries Division column", "Division" in hdr, str(hdr))
check("no temp files left behind",
      not [n for n in os.listdir(T._paths()["snapshots"]) if n.startswith(".snap-")])
rp = T.write_report(rows, "2026-12-31")
check("report written 0600", oct(os.stat(rp).st_mode)[-3:] == "600")
check("dirs are 0700", oct(os.stat(T._paths()["snapshots"]).st_mode)[-3:] == "700")
r2 = T.diff_snapshots(old, new, d2)
check("diff is deterministic", r2 == rows)

# ---------------------------------------------------------------- auth safety
print("\n[auth safety]")
try:
    T.Session("", None, None); check("missing subdomain raises", False)
except T.TrackerError:
    check("missing subdomain raises", True)
s = T.Session("acme", None, "SECRETKEY123")
try:
    s.get_web("/x"); check("web path without cookie raises", False)
except T.TrackerError as e:
    check("web path without cookie raises", "SECRETKEY123" not in str(e))
check("redact strips query string",
      T._redact("https://h/p?orgChartOptions[]=x&t=1") == "https://h/p")
# behavioral: capture every URL and header the client would actually send
SEEN = []
def _spy(self, url, headers):
    SEEN.append((url, headers))
    return 200, b'{"employees":[{"id":1,"displayName":"A"}]}', "application/json"
_real = T.Session._open
T.Session._open = _spy
try:
    sp = T.Session("acme", "SESSIONCOOKIEVALUE", "APIKEYVALUE")
    T.fetch_roster_api(sp)
    sp.get_web("/employees/exportable_org_chart/pdf/9", T.chart_params(6) + [("type", "pdf")])
finally:
    T.Session._open = _real
urls = [u for u, _ in SEEN]
check("credentials never appear in a URL",
      not any("APIKEYVALUE" in u or "SESSIONCOOKIEVALUE" in u for u in urls),
      f"{len(urls)} urls captured")
check("api key sent as Basic auth header",
      any("Authorization" in h and h["Authorization"].startswith("Basic ")
          for _, h in SEEN))
check("cookie sent as Cookie header",
      any(h.get("Cookie") == "SESSIONCOOKIEVALUE" for _, h in SEEN))
check("chart options present in the export URL",
      any("orgChartOptions" in u and "selectedLevel=6" in u for u in urls))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "org_tracker.py")).read()
check("redirects are not followed", "redirect_request" in src and "return None" in src)

# --------------------------------------------------------- the core anti-claim
print("\n[anti-claim: auth failure must never look like 'no changes']")
tmp2 = tempfile.mkdtemp(prefix="tracker-authfail-")
os.environ["BHR_ORG_TRACKER_HOME"] = tmp2
T.ensure_dirs()
# any pre-existing snapshot will do; the claim is that a failed run adds none
T.write_snapshot(people([("1", "Existing P", "")]), "2026-07-09")
before = sorted(os.listdir(T._paths()["snapshots"]))

_real = T.Session._open
for label, status, body in [
    ("302 login redirect", 302, b""),
    ("403 forbidden", 403, b""),
    ("200 login page", 200, b"<html><head><title>Log In - BambooHR</title>"),
]:
    T.Session._open = lambda self, url, headers, _s=status, _b=body: (_s, _b, "text/html")
    try:
        sp = T.Session("acme", "EXPIREDCOOKIE", None)
        sp.get_web("/employees/exportable_org_chart/csv/1")
        check(f"{label} raises", False, "no raise")
    except T.TrackerError as e:
        check(f"{label} raises", True, str(e)[:48])
    finally:
        T.Session._open = _real

# a full run against an expired session must exit non-zero and write nothing
T.Session._open = lambda self, url, headers: (302, b"", "text/html")
try:
    os.environ["BHR_SUBDOMAIN"] = "acme"
    os.environ["BHR_SESSION_COOKIE"] = "EXPIREDCOOKIE"
    os.environ.pop("BHR_PERSONAL_API_KEY", None)
    rc = T.main(["--run", "--root", "1", "--day", "2026-09-04"])
    check("expired session: run exits non-zero", rc == 1, f"rc={rc}")
    check("expired session: no snapshot written",
          sorted(os.listdir(T._paths()["snapshots"])) == before,
          str(sorted(os.listdir(T._paths()["snapshots"]))))
    check("expired session: no report written",
          not os.path.exists(T.report_path("2026-09-04")))
finally:
    T.Session._open = _real
    for k in ("BHR_SUBDOMAIN", "BHR_SESSION_COOKIE"):
        os.environ.pop(k, None)

# an empty roster is an error, not an empty company
try:
    T.roster_from_csv_bytes(b"PersonID,Name\n")
    check("empty roster raises", False, "no raise")
except T.TrackerError:
    check("empty roster raises", True)
try:
    T.roster_from_csv_bytes(b"Foo,Bar\n1,2\n")
    check("roster without PersonID raises", False, "no raise")
except T.TrackerError:
    check("roster without PersonID raises", True)
shutil.rmtree(tmp2, ignore_errors=True)

# --------------------------------------------------------------- email
print("\n[email]")
import smtplib, ssl

SECRET = "app-password-do-not-leak"
os.environ.update({
    "BHR_SMTP_USER": "steve@example.com",
    "BHR_SMTP_PASSWORD": SECRET,
    "BHR_MAIL_TO": "steve@example.com",
})
os.environ.pop("BHR_MAIL_ATTACH_CSV", None)

class FakeSMTP:
    """Captures everything a real server would see."""
    instances = []
    def __init__(self, host, port, context=None, timeout=None):
        self.host, self.port, self.context = host, port, context
        self.tls = False; self.logins = []; self.sent = []
        FakeSMTP.instances.append(self)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self, context=None): self.tls = True
    def login(self, u, p): self.logins.append((u, p))
    def send_message(self, m): self.sent.append(m)

def with_fake(port, fn):
    FakeSMTP.instances = []
    rs, rp = smtplib.SMTP_SSL, smtplib.SMTP
    T.smtplib.SMTP_SSL = FakeSMTP; T.smtplib.SMTP = FakeSMTP
    os.environ["BHR_SMTP_PORT"] = str(port)
    try:
        fn()
    finally:
        T.smtplib.SMTP_SSL, T.smtplib.SMTP = rs, rp
    return FakeSMTP.instances

rows_sample = [r for r in rows][:40]
inst = with_fake(465, lambda: T.email_report("2026-09-04", rows_sample, None))
check("implicit TLS on 465 uses SMTP_SSL", len(inst) == 1 and inst[0].port == 465
      and inst[0].context is not None and not inst[0].tls)
msg = inst[0].sent[0]
check("message addressed and subject-lined",
      msg["To"] == "steve@example.com" and "departure" in msg["Subject"], msg["Subject"])
check("multipart: plain text and HTML", msg.is_multipart()
      and {pp.get_content_type() for pp in msg.walk()} >= {"text/plain", "text/html"})
body = msg.get_body(preferencelist=("plain",)).get_content()
htmlb = msg.get_body(preferencelist=("html",)).get_content()
check("body groups by department", "by department" in body)
check("html has a table with manager column", "<table" in htmlb and "Manager" in htmlb)
check("no attachment by default (PII opt-in)", len(list(msg.iter_attachments())) == 0)

inst = with_fake(587, lambda: T.email_report("2026-09-04", rows_sample, None))
check("STARTTLS on 587", len(inst) == 1 and inst[0].tls is True)
check("login used the configured user", inst[0].logins == [("steve@example.com", SECRET)])

# the password must not escape into the message or the log
full = str(msg)
check("password absent from the sent message", SECRET not in full)
logtxt = open(T._paths()["log"]).read() if os.path.exists(T._paths()["log"]) else ""
check("password absent from the log", SECRET not in logtxt)
check("describe() never prints the password", SECRET not in T.MailConfig().describe())

# attachment only when explicitly enabled
os.environ["BHR_MAIL_ATTACH_CSV"] = "1"
inst = with_fake(465, lambda: T.email_report("2026-09-04", rows_sample, rp))
att = list(inst[0].sent[0].iter_attachments())
check("CSV attaches when opted in", len(att) == 1 and att[0].get_filename().endswith(".csv"),
      str([a.get_filename() for a in att]))
os.environ.pop("BHR_MAIL_ATTACH_CSV", None)

# a no-change day still sends a heartbeat
inst = with_fake(465, lambda: T.email_report("2026-09-04", [], None))
check("no-change day still emails a heartbeat",
      len(inst) == 1 and "no org changes" in inst[0].sent[0]["Subject"],
      inst[0].sent[0]["Subject"])

# a failed run emails
inst = with_fake(465, lambda: T.email_failure("2026-09-04", "session rejected (HTTP 302)"))
fm = inst[0].sent[0]
check("failure email sent", "FAILED" in fm["Subject"], fm["Subject"])
check("failure email explains nothing was checked",
      "nothing was checked" in fm.get_content())

# auth rejection surfaces actionable text, not the password
class AuthFail(FakeSMTP):
    def login(self, u, p): raise smtplib.SMTPAuthenticationError(535, b"denied")
FakeSMTP.instances = []
_rs = smtplib.SMTP_SSL; T.smtplib.SMTP_SSL = AuthFail
try:
    T.send_message(T.MailConfig(), T.build_message(T.MailConfig(), "s", "t"))
    check("SMTP auth failure raises", False, "no raise")
except T.TrackerError as e:
    check("SMTP auth failure raises", True)
    check("auth error names app password, hides secret",
          "app-specific password" in str(e) and SECRET not in str(e))
finally:
    T.smtplib.SMTP_SSL = _rs

# unconfigured email is a skip on the report path, never a crash
for k in ("BHR_SMTP_USER", "BHR_SMTP_PASSWORD", "BHR_MAIL_TO"):
    os.environ.pop(k, None)
check("unconfigured email does not raise on report path",
      T.email_report("2026-09-04", rows_sample, None) is None)

# --------------------------------------------------------------- notification
print("\n[macOS notification]")
CALLS = []
_real_run = T.subprocess.run

class FakeProc:
    def __init__(self, rc=0, err=b""):
        self.returncode, self.stderr, self.stdout = rc, err, b""

def fake_run(args, **kw):
    CALLS.append(list(args))
    exe = os.path.basename(args[0])
    return FakeProc(0) if exe in ("terminal-notifier", "osascript", "open") else FakeProc(1)

def with_notifier(present, fn, run=fake_run):
    global CALLS
    CALLS = []
    rw, rwhich, rplat = T.subprocess.run, T.shutil.which, T.sys.platform
    T.subprocess.run = run
    T.shutil.which = (lambda n: "/opt/homebrew/bin/terminal-notifier"
                      if n == "terminal-notifier" and present else None)
    T.sys.platform = "darwin"
    try:
        return fn()
    finally:
        T.subprocess.run, T.shutil.which, T.sys.platform = rw, rwhich, rplat

sample = [
    {"change_type": "departed", "name": "A Person", "department": "Platform Engineering",
     "job_title": "Eng", "division": "Tech", "supervisor_name": "M", "person_id": "1"},
    {"change_type": "new_hire", "name": "B Person", "department": "Security",
     "job_title": "Sec", "division": "Tech", "supervisor_name": "M", "person_id": "2"},
]
html = T.write_html_report(sample, "2026-09-09")
check("html report written 0600", oct(os.stat(html).st_mode)[-3:] == "600")
hb = open(html).read()
check("html report has a table of changes", "<table" in hb and "A Person" in hb)

ok = with_notifier(True, lambda: T.notify_macos("2026-09-09", sample, html))
check("clickable path used when terminal-notifier present", ok and len(CALLS) == 1
      and CALLS[0][0].endswith("terminal-notifier"), str(CALLS[:1])[:90])
argv = CALLS[0]
check("click action passes the report as a file URL",
      "-open" in argv and argv[argv.index("-open") + 1].startswith("file://")
      and argv[argv.index("-open") + 1].endswith(".html"))
banner = " ".join(argv)
check("banner carries counts and departments, NOT individual names",
      "1 departure" in banner and "Platform Engineering" in banner
      and "A Person" not in banner and "B Person" not in banner)

ok = with_notifier(False, lambda: T.notify_macos("2026-09-09", sample, html))
check("falls back to osascript when absent", ok and len(CALLS) == 1
      and CALLS[0][0] == "osascript")
check("fallback names the report file since it cannot be clicked",
      "changes-2026-09-09.html" in " ".join(CALLS[0]))

# permission failure -> accurate diagnosis, then fallback
def perm_denied(args, **kw):
    CALLS.append(list(args))
    if os.path.basename(args[0]) == "terminal-notifier":
        return FakeProc(3, b"Could not request notification permission: "
                           b"Notifications are not allowed for this application")
    return FakeProc(0)
ok = with_notifier(True, lambda: T.notify_macos("2026-09-09", sample, html), run=perm_denied)
check("permission failure still notifies via fallback", ok and len(CALLS) == 2)
logtxt = open(T._paths()["log"]).read()
check("permission failure diagnosed with the actual fix",
      "lacks notification permission" in logtxt and "open /opt/homebrew" in logtxt)

# untrusted department text must never reach a shell
evil = [{"change_type": "departed", "name": "X", "person_id": "9", "division": "Tech",
         "job_title": "T", "supervisor_name": "M",
         "department": 'Ops"; rm -rf /tmp/pwned; echo "'}]
ok = with_notifier(True, lambda: T.notify_macos("2026-09-09", evil, None))
check("notifier args are a list, never a shell string",
      any(evil[0]["department"] in a for a in CALLS[0]),
      "department passed as one argv element")
ok = with_notifier(False, lambda: T.notify_macos("2026-09-09", evil, None))
osa = CALLS[0]
check("osascript path quotes embedded quotes",
      osa[0] == "osascript" and osa[1] == "-e" and '\\"' in osa[2],
      "AppleScript string escaped")
check("no shell=True in any notification call", "shell=True" not in open("org_tracker.py").read())

# no-change day still notifies, and says so
ok = with_notifier(True, lambda: T.notify_macos("2026-09-09", [], html))
check("no-change day notifies a heartbeat", ok and "No changes today" in " ".join(CALLS[0]))

# non-macOS is a silent no-op, never an error
rplat = T.sys.platform
T.sys.platform = "linux"
try:
    check("non-macOS posts nothing and does not raise",
          not T.notify_macos("2026-09-09", sample, html))
finally:
    T.sys.platform = rplat

# an Intel Homebrew (/usr/local) terminal-notifier gets the right app path
_rwhich = T.shutil.which
T.shutil.which = lambda n: "/usr/local/bin/terminal-notifier" if n == "terminal-notifier" else None
try:
    _app = T.notifier_app() if hasattr(T, "notifier_app") else getattr(T, "NOTIFIER_APP", "")
finally:
    T.shutil.which = _rwhich
check("terminal-notifier app path follows the Homebrew prefix it was installed under",
      _app == "/usr/local/opt/terminal-notifier/terminal-notifier.app", _app)

# --test-notify writes its sample beside, never over, today's real report
_real_page = os.path.join(T._paths()["reports"], "changes-2026-10-06.html")
with open(_real_page, "w") as _fh:
    _fh.write("<html>REAL REPORT</html>")
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _rc = with_notifier(False, lambda: T.main(["--test-notify", "--day", "2026-10-06"]))
check("--test-notify leaves today's real report untouched",
      open(_real_page).read() == "<html>REAL REPORT</html>", f"rc={_rc}")
check("--test-notify does not promise a click when only the plain fallback posted",
      _rc == 0 and "click it to open" not in _out.getvalue()
      and "NOT clickable" in _out.getvalue(), _out.getvalue().strip()[-90:])
T.subprocess.run = _real_run

# --------------------------------------------------------------- roll-ups
print("\n[roll-ups]")
import datetime as _dt
D = _dt.date
check("first weekday Sep 2026 is Tue 1st", T.first_weekday(2026, 9) == D(2026, 9, 1))
check("first weekday Aug 2026 skips Sat/Sun to Mon 3rd", T.first_weekday(2026, 8) == D(2026, 8, 3))
check("first weekday Nov 2026 is Mon 2nd", T.first_weekday(2026, 11) == D(2026, 11, 2))
check("first weekday Feb 2026 is Mon 2nd", T.first_weekday(2026, 2) == D(2026, 2, 2))
_fw = [T.first_weekday(y, m) for y in range(2024, 2031) for m in range(1, 13)]
check("first weekday is never a weekend, 2024-2030", all(d.weekday() < 5 for d in _fw))
check("first weekday is always within the first 3 days", all(d.day <= 3 for d in _fw))

w = T.weekly_period(D(2026, 9, 9))  # a Wednesday
check("weekly anchors on this week's Monday",
      w["anchor"] == "2026-09-07" and w["start_bound"] == "2026-08-31"
      and w["end_bound"] == "2026-09-07", str(w))
check("a Monday anchors on itself", T.weekly_period(D(2026, 9, 7))["anchor"] == "2026-09-07")
m = T.monthly_period(D(2026, 10, 1))
check("monthly covers the previous calendar month",
      m["anchor"] == "2026-09" and m["start_bound"] == "2026-08-31"
      and m["end_bound"] == "2026-09-30" and m["label"] == "September 2026", str(m))
m = T.monthly_period(D(2026, 1, 4))
check("monthly wraps the year",
      m["anchor"] == "2025-12" and m["start_bound"] == "2025-11-30"
      and m["end_bound"] == "2025-12-31", str(m))

# synthetic history with skipped days (the laptop was closed 09-01, 09-02, 09-08)
_prev_home = os.environ.get("BHR_ORG_TRACKER_HOME")
rhome = tempfile.mkdtemp(prefix="tracker-rollup-")
os.environ["BHR_ORG_TRACKER_HOME"] = rhome
def _snap(day, ids):
    T.write_snapshot(people([(str(i), f"P{i}", "") for i in ids]), day)
_snap("2026-08-28", range(1, 11))   # Fri
_snap("2026-08-31", range(1, 11))   # Mon  (boundary)
_snap("2026-09-03", range(2, 12))   # Thu: 1 left, 11 joined
_snap("2026-09-06", range(2, 12))   # Sun
_snap("2026-09-07", range(3, 13))   # Mon: 2 left, 12 joined  (boundary)
_snap("2026-09-09", range(3, 14))   # Wed: 13 joined -- after the anchor, must not appear

res = T.resolve_period(T.weekly_period(D(2026, 9, 9)))
check("weekly resolves each bound to the latest snapshot on/before it",
      res["start"][0] == "2026-08-31" and res["end"][0] == "2026-09-07",
      f"{res['start'][0]} -> {res['end'][0]}")
check("period span lists every snapshot between the bounds inclusive",
      [d for d, _ in res["span"]] == ["2026-08-31", "2026-09-03", "2026-09-06", "2026-09-07"])
csvp = T.run_rollup(T.weekly_period(D(2026, 9, 9)))
with open(csvp, newline="") as fh:
    kinds = {(r["change_type"], r["person_id"]) for r in csv.DictReader(fh)}
check("weekly diff spans the whole week, not just the last day",
      kinds == {("departed", "1"), ("departed", "2"), ("new_hire", "11"), ("new_hire", "12")},
      str(sorted(kinds)))
check("changes after the anchor are excluded", ("new_hire", "13") not in kinds)
check("weekly report named by its anchor Monday",
      os.path.basename(csvp) == "rollup-weekly-2026-09-07.csv", csvp)
check("weekly html written beside the csv, 0600",
      os.path.exists(csvp[:-4] + ".html") and oct(os.stat(csvp).st_mode)[-3:] == "600")

# consecutive periods share their boundary snapshot -- no gap, no double count
p_prev = T.resolve_period(T.weekly_period(D(2026, 9, 2)))
p_this = T.resolve_period(T.weekly_period(D(2026, 9, 9)))
check("end of one week == start of the next, across a skipped Monday-adjacent day",
      p_prev["end"][0] == p_this["start"][0] == "2026-08-31")
p_next = T.resolve_period(T.weekly_period(D(2026, 9, 16)))
check("end of this week == start of next week even though 09-14 has no snapshot",
      p_this["end"][0] == "2026-09-07" and p_next["start"][0] == "2026-09-07"
      and p_next["end"][0] == "2026-09-09")

# --rollup auto: due / idempotent / catch-up / never before the first weekday
due = T.rollups_due(D(2026, 9, 9))
check("auto: weekly already produced -> not due again",
      not any(p["kind"] == "weekly" for p in due))
check("auto: August monthly due on 09-09 (first weekday passed, file absent)",
      [p["anchor"] for p in due] == ["2026-08"], str([p["anchor"] for p in due]))
check("auto: nothing due on Sat Aug 1 (before the first weekday, no snapshot yet)",
      T.rollups_due(D(2026, 8, 1)) == [])
check("auto: Mon Aug 3 -- July monthly not due, tracker did not exist",
      T.rollups_due(D(2026, 8, 3)) == [])
resm = T.resolve_period(due[0])
check("history younger than the period falls back to the earliest snapshot with a note",
      resm["start"][0] == "2026-08-28" and "History begins 2026-08-28" in resm["note"],
      resm["note"])
csvm = T.run_rollup(due[0])
check("monthly report named by month", os.path.basename(csvm) == "rollup-monthly-2026-08.csv")
check("auto: re-run the same day is a no-op", T.rollups_due(D(2026, 9, 9)) == [])
check("auto catch-up: a Wednesday after a missed Monday still anchors on that Monday",
      [p["anchor"] for p in T.rollups_due(D(2026, 9, 16))] == ["2026-09-14"])
try:
    T.resolve_period(T.monthly_period(D(2026, 8, 3)))
    check("period with no snapshot at all raises", False, "no raise")
except T.TrackerError as e:
    check("period with no snapshot at all raises", "no snapshot on or before 2026-07-31" in str(e))
_single = T.resolve_period({"kind": "weekly", "noun": "week", "anchor": "2026-08-31",
                            "start_bound": "2026-08-24", "end_bound": "2026-08-28",
                            "label": "week ending 2026-08-31"})
check("single-snapshot period says there is nothing to compare",
      _single["start"][0] == _single["end"][0] == "2026-08-28"
      and "nothing to compare" in _single["note"], _single["note"])

# rendering + email through the fake SMTP
os.environ.update({"BHR_SMTP_USER": "steve@example.com", "BHR_SMTP_PASSWORD": SECRET,
                   "BHR_MAIL_TO": "steve@example.com"})
os.environ.pop("BHR_MAIL_ATTACH_CSV", None)
inst = with_fake(465, lambda: T.run_rollup(T.weekly_period(D(2026, 9, 9)), email=True))
msg = inst[0].sent[0]
check("weekly subject carries counts, headcount delta and the period",
      "BambooHR weekly: 2 departure(s), 2 new hire(s), headcount 10→10 (+0) — week ending 2026-09-07"
      == msg["Subject"], msg["Subject"])
body = msg.get_body(preferencelist=("plain",)).get_content()
htmlb = msg.get_body(preferencelist=("html",)).get_content()
check("body carries headcount start -> end with signed delta", "Headcount: 10 -> 10 (+0)" in body)
check("body names the compared snapshots", "Compared snapshots 2026-08-31 -> 2026-09-07" in body)
check("body carries a per-snapshot headcount trend",
      "Headcount by snapshot:" in body and "  2026-09-03  10" in body)
check("body still groups by department", "by department" in body)
check("html has the trend table and the manager column",
      "Headcount by snapshot" in htmlb and "<th>Snapshot</th>" in htmlb and "Manager" in htmlb)
check("html title names the roll-up", "BambooHR weekly org changes — week ending 2026-09-07" in htmlb)
check("password absent from the roll-up mail", SECRET not in str(msg))
check("no attachment by default on roll-ups", len(list(msg.iter_attachments())) == 0)
inst = with_fake(465, lambda: T.run_rollup(_single, email=True))
check("single-snapshot roll-up is labelled insufficient history, not zero changes",
      "insufficient history" in inst[0].sent[0]["Subject"], inst[0].sent[0]["Subject"])
inst = with_fake(465, lambda: T.email_failure("2026-09-09", "boom", what="roll-up"))
fm = inst[0].sent[0]
check("roll-up failure email is distinct from the daily one",
      "roll-up FAILED" in fm["Subject"] and "No roll-up report was produced" in fm.get_content())
# the daily renderer is byte-identical when no extras are passed
_t, _h = T._render_bodies("2026-09-09", [], "")
check("daily renderer unchanged without roll-up extras",
      _t.startswith("BambooHR org changes for 2026-09-09") and "Headcount" not in _t
      and "Org changes — 2026-09-09" in _h and "Headcount" not in _h)
# CLI dispatch
_rc = T.main(["--rollup", "auto", "--day", "2026-09-09"])
check("--rollup auto exits 0 when nothing is due", _rc == 0)
_rc = T.main(["--rollup", "monthly", "--day", "2026-08-03"])
check("--rollup monthly with no history exits 1", _rc == 1)
for k in ("BHR_SMTP_USER", "BHR_SMTP_PASSWORD", "BHR_MAIL_TO", "BHR_SMTP_PORT"):
    os.environ.pop(k, None)
if _prev_home is not None:
    os.environ["BHR_ORG_TRACKER_HOME"] = _prev_home
shutil.rmtree(rhome, ignore_errors=True)

# --------------------------------------------------------- linux portability
print("\n[linux portability]")
src_all = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "org_tracker.py")).read()
for bad in ("launchctl", "/usr/libexec", "pbcopy", "sw_vers"):
    check(f"no macOS dependency: {bad}", bad not in src_all)

# osascript/open ARE referenced, for notifications. What matters is that they
# only run behind an is_macos() guard, which the behavioural check above
# already exercises; this pins the guard's position so it cannot drift.
import ast as _ast
_tree = _ast.parse(src_all)
_fn = next((n for n in _ast.walk(_tree)
            if isinstance(n, _ast.FunctionDef) and n.name == "notify_macos"), None)
check("notify_macos exists", _fn is not None)
_guard = None
for _st in (_fn.body if _fn else []):
    if isinstance(_st, _ast.If):
        _guard = _ast.dump(_st.test)
        break
check("macOS calls sit behind an is_macos() guard",
      _guard is not None and "is_macos" in _guard)
_mac_fns = {n.name for n in _ast.walk(_tree) if isinstance(n, _ast.FunctionDef)
            and ("osascript" in _ast.dump(n) or '"open"' in _ast.dump(n))}
check("osascript/open confined to the notification helpers",
      _mac_fns <= {"notify_macos", "register_notifier"}, str(sorted(_mac_fns)))
check("no shell=True anywhere", "shell=True" not in src_all)
check("pdftotext is checked before use, not assumed",
      "shutil.which(\"pdftotext\")" in src_all)
import ast
tree = ast.parse(src_all)
mods = {n.module.split(".")[0] for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module}
mods |= {a.name.split(".")[0] for n in ast.walk(tree)
         if isinstance(n, ast.Import) for a in n.names}
stdlib = set(getattr(sys, "stdlib_module_names", ()))
nonstd = {m for m in mods if stdlib and m not in stdlib and m != "org_tracker"}
check("every import is stdlib", not nonstd, str(sorted(nonstd)))

# --------------------------------------------------- transient network faults
print("\n[transient network faults]")

# The incident these cover: a read-phase timeout is a bare TimeoutError, not a
# urllib.error.URLError. It escaped _open's handler, killed the process, and
# the run produced no snapshot, no failure mail and no notification -- which
# is indistinguishable from the job never having fired.


class _FakeOpener:
    """Stands in for urllib's opener. Raises a scripted sequence, then 200s."""

    def __init__(self, faults):
        self.faults = list(faults)
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        if self.faults:
            raise self.faults.pop(0)
        return _FakeResponse()


class _FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def read(self):
        return b'{"employees": []}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _with_opener(faults):
    """Swap in a scripted opener and a no-op sleep; returns the fake."""
    fake = _FakeOpener(faults)
    T.urllib.request.build_opener = lambda *a, **k: fake
    T.time.sleep = lambda n: None
    return fake


_real_build_opener = T.urllib.request.build_opener
_real_sleep = T.time.sleep
_sess = T.Session("acme", None, "key-not-used-offline")

# a read timeout is converted, not propagated
_fake = _with_opener([TimeoutError("The read operation timed out")] * T.HTTP_RETRIES)
try:
    _sess.get_api("/v1/employees/directory")
    _raised = None
except T.TrackerError as exc:
    _raised = exc
except TimeoutError as exc:
    _raised = exc
check("read timeout becomes TrackerError, never a bare TimeoutError",
      isinstance(_raised, T.TrackerError), type(_raised).__name__)
check("timeout message names the timeout and the attempt count",
      _raised is not None and "timed out" in str(_raised)
      and f"{T.HTTP_RETRIES} attempts" in str(_raised), str(_raised)[:90])
check("a timing-out request is retried, not attempted once",
      _fake.calls == T.HTTP_RETRIES, f"{_fake.calls} calls")

# a transient fault that clears is retried into success
_fake = _with_opener([TimeoutError("boom")])
_body = _sess.get_api("/v1/employees/directory")
check("a fault that clears on retry yields the real response",
      _body == b'{"employees": []}' and _fake.calls == 2, f"{_fake.calls} calls")

# a cert failure is NOT retried -- it should fall back immediately
_certerr = T.ssl.SSLCertVerificationError("certificate verify failed: Hostname mismatch")
_fake = _with_opener([T.urllib.error.URLError(_certerr)] * T.HTTP_RETRIES)
try:
    _sess.get_api("/v1/employees/directory")
    _raised = None
except T.TrackerError as exc:
    _raised = exc
check("a certificate failure raises TrackerError", isinstance(_raised, T.TrackerError))
check("a certificate failure is not retried (falls back at once)",
      _fake.calls == 1, f"{_fake.calls} calls")

# an HTTP status is a real response, never retried
_fake = _with_opener([])
_sess.get_api("/v1/employees/directory")
check("a clean request makes exactly one call", _fake.calls == 1, f"{_fake.calls} calls")

T.urllib.request.build_opener = _real_build_opener
T.time.sleep = _real_sleep

check("_transient retries timeouts", T._transient(TimeoutError()))
check("_transient retries DNS failures", T._transient(T.socket.gaierror()))
check("_transient retries refused connections", T._transient(ConnectionRefusedError()))
check("_transient does not retry certificate failures",
      not T._transient(T.ssl.SSLCertVerificationError("bad cert")))

# the network wait must never be fatal, and must not hang without a host
check("wait_for_network with no host returns False immediately",
      T.wait_for_network(None) is False)
_t0 = T.time.monotonic()
check("wait_for_network gives up inside its budget rather than hanging",
      T.wait_for_network("127.0.0.1", port=1, budget=0) is False
      and T.time.monotonic() - _t0 < 10)

# --------------------------------------------------- log de-duplication
print("\n[log de-duplication]")

_lhome = tempfile.mkdtemp(prefix="tracker-log-")
_prev_home = os.environ.get("BHR_ORG_TRACKER_HOME")
os.environ["BHR_ORG_TRACKER_HOME"] = _lhome
T._STDOUT_IS_LOG = None
try:
    _logpath = T._paths()["log"]
    os.makedirs(os.path.dirname(_logpath), exist_ok=True)
    # Reproduce launchd: stdout IS the log file.
    _saved = sys.stdout
    with open(_logpath, "a", encoding="utf-8") as _fh:
        sys.stdout = _fh
        T._STDOUT_IS_LOG = None
        T.log("one line only")
    sys.stdout = _saved
    _lines = [l for l in open(_logpath, encoding="utf-8") if "one line only" in l]
    check("stdout redirected to the log yields ONE line, not two",
          len(_lines) == 1, f"{len(_lines)} lines")

    # A normal pipe still prints: `--run | tee` must keep working.
    T._STDOUT_IS_LOG = None
    _cap = io.StringIO()
    _saved = sys.stdout
    sys.stdout = _cap
    T.log("piped line")
    sys.stdout = _saved
    check("a pipe that is not the log still prints", "piped line" in _cap.getvalue())
finally:
    sys.stdout = sys.__stdout__
    if _prev_home is None:
        os.environ.pop("BHR_ORG_TRACKER_HOME", None)
    else:
        os.environ["BHR_ORG_TRACKER_HOME"] = _prev_home
    T._STDOUT_IS_LOG = None
    shutil.rmtree(_lhome, ignore_errors=True)

# --------------------------------------------- shrink guard leaves no trace
print("\n[shrink guard: a refused roster leaves nothing on disk]")
import json, shlex, stat

_ENV_KEYS = ("BHR_SUBDOMAIN", "BHR_PERSONAL_API_KEY", "BHR_SESSION_COOKIE", "BHR_ALLOW_SHRINK",
             "BHR_ORG_TRACKER_HOME", "BHR_SMTP_USER", "BHR_SMTP_PASSWORD", "BHR_MAIL_TO",
             "BHR_SMTP_PORT")
_saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
_real_open, _real_wait = T.Session._open, T.wait_for_network
T.wait_for_network = lambda *a, **k: True
os.environ.update({"BHR_SUBDOMAIN": "acme", "BHR_PERSONAL_API_KEY": "offline-fake-key"})
for _k in ("BHR_SESSION_COOKIE", "BHR_ALLOW_SHRINK"):
    os.environ.pop(_k, None)


def _directory(ids):
    return json.dumps({"employees": [{"id": i, "displayName": f"P{i}", "department": "D",
                                      "jobTitle": "J"} for i in ids]}).encode()


def _serve(status, body):
    T.Session._open = lambda self, url, headers: (status, body, "application/json")


def _main(*argv):
    """T.main with its log lines kept off the screen. A raw exception is
    returned, not raised, so a regression shows up as a FAIL, not a crash."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return T.main(list(argv))
    except Exception as exc:  # noqa: BLE001 -- the point is to see it
        return exc


def _report_kinds(day):
    if not os.path.exists(T.report_path(day)):
        return None
    with open(T.report_path(day), newline="") as fh:
        return [r["change_type"] for r in csv.DictReader(fh)]


_ghome = tempfile.mkdtemp(prefix="tracker-guard-")
os.environ["BHR_ORG_TRACKER_HOME"] = _ghome
_snapdir = T._paths()["snapshots"]
_marker = os.path.join(_snapdir, "accepted-drops.txt")   # the documented location


def _marker_lines(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


try:
    _serve(200, _directory(range(1, 101)))
    _main("--run", "--day", "2026-09-07")                       # Mon: 100 people
    _serve(200, _directory(range(1, 11)))
    _rc = _main("--run", "--day", "2026-09-08")                 # truncated pull: 10
    check("shrink guard: a truncated pull exits 1", _rc == 1, repr(_rc))
    check("shrink guard: the refused roster leaves NO snapshot on disk",
          not os.path.exists(T.snapshot_path("2026-09-08")), str(sorted(os.listdir(_snapdir))))
    _serve(200, _directory(range(1, 101)))
    _main("--run", "--day", "2026-09-09")                       # full roster again
    _kinds = _report_kinds("2026-09-09")
    check("shrink guard: the day after a refusal reports no phantom new hires",
          _kinds == [], f"{(_kinds or []).count('new_hire')} new_hire rows")
    with open(T.snapshot_path("2026-09-09"), "rb") as _fh:
        _today = _fh.read()
    _serve(200, _directory(range(1, 11)))
    _rc = _main("--run", "--day", "2026-09-09")                 # same-day rerun, truncated
    with open(T.snapshot_path("2026-09-09"), "rb") as _fh:
        check("shrink guard: a refused rerun does not replace today's snapshot",
              _rc == 1 and _fh.read() == _today, repr(_rc))

    # an accepted drop is remembered, so the roll-ups spanning it pass
    os.environ["BHR_ALLOW_SHRINK"] = "1"
    _serve(200, _directory(range(1, 41)))
    _rc = _main("--run", "--day", "2026-09-10")                 # real drop to 40, accepted
    os.environ.pop("BHR_ALLOW_SHRINK")
    check("accepted drop: run with BHR_ALLOW_SHRINK=1 succeeds and keeps the snapshot",
          _rc == 0 and os.path.exists(T.snapshot_path("2026-09-10")), repr(_rc))
    _markers = [n for n in os.listdir(_snapdir) if not n.startswith(("snapshot-", "."))]
    check("accepted drop: remembered in a marker file beside the snapshots, 0600",
          len(_markers) == 1 and stat.S_IMODE(os.stat(os.path.join(_snapdir, _markers[0])).st_mode)
          == 0o600, str(_markers))
    os.environ["BHR_ALLOW_SHRINK"] = "1"
    _serve(200, _directory(range(1, 31)))
    _rc = _main("--run", "--day", "2026-09-10")                 # accepted rerun: 30
    os.environ.pop("BHR_ALLOW_SHRINK")
    check("accepted drop: same-day accepted rerun updates the stored counts",
          _rc == 0 and _marker_lines(_marker) == ["2026-09-10 100 30"], str(_marker_lines(_marker)))
    check("accepted drop: atomic replacement keeps the marker mode at 0600",
          os.path.exists(_marker) and stat.S_IMODE(os.stat(_marker).st_mode) == 0o600)
    with open(_marker, "a", encoding="utf-8") as _fh:   # hand-edited or damaged lines
        _fh.write("garbage\n2026-09-11 not-a-count 1\n2026-13-45 100 10\n")
    _rc = _main("--rollup", "weekly", "--day", "2026-09-14")   # spans 09-07 (100) -> 09-10 (30)
    check("accepted drop: malformed marker lines are ignored, never fatal",
          not isinstance(_rc, Exception), repr(_rc))
    check("accepted drop: the weekly roll-up spanning it passes without the override",
          _rc == 0 and os.path.exists(T.rollup_paths(T.weekly_period(
              __import__("datetime").date(2026, 9, 14)))[0]), repr(_rc))
    _rc = _main("--diff-only")
    check("accepted drop: --diff-only across it passes without the override", _rc == 0, repr(_rc))
    _serve(200, _directory(range(1, 81)))
    _rc = _main("--run", "--day", "2026-09-10")                 # fixed rerun: 80
    check("accepted drop: same-day fixed rerun retracts the day's acceptance, and the "
          "emptied marker file is deleted",
          _rc == 0 and _marker_lines(_marker) is None, str(_marker_lines(_marker)))
finally:
    T.Session._open = _real_open

# a drop nobody accepted still refuses in a roll-up (here: two sub-50% steps)
_uhome = tempfile.mkdtemp(prefix="tracker-unaccepted-")
os.environ["BHR_ORG_TRACKER_HOME"] = _uhome
for _day, _n in (("2026-08-31", 100), ("2026-09-03", 60), ("2026-09-05", 35)):
    T.write_snapshot(people([(str(i), f"P{i}", "") for i in range(1, _n + 1)]), _day)
_rc = _main("--rollup", "weekly", "--day", "2026-09-07")
check("un-accepted drop: a roll-up spanning it still refuses", _rc == 1, repr(_rc))

# An accepted drop only accounts for its own ratio. Later sub-50% daily steps
# can still compound into another guarded decline across the full roll-up.
_shome = tempfile.mkdtemp(prefix="tracker-spanning-drop-")
os.environ["BHR_ORG_TRACKER_HOME"] = _shome
_serve(200, _directory(range(1, 101)))
_main("--run", "--day", "2026-08-31")
os.environ["BHR_ALLOW_SHRINK"] = "1"
_serve(200, _directory(range(1, 41)))
_main("--run", "--day", "2026-09-01")
os.environ.pop("BHR_ALLOW_SHRINK")
for _day, _n in (("2026-09-03", 30), ("2026-09-06", 15)):
    _serve(200, _directory(range(1, _n + 1)))
    _main("--run", "--day", _day)
_rc = _main("--rollup", "weekly", "--day", "2026-09-07")
check("accepted drop: roll-up spanning it plus a later un-accepted decline refuses",
      _rc == 1, repr(_rc))

# The marker interval is open at the start: a drop accepted on the starting
# snapshot cannot excuse the later decline.
_bhome = tempfile.mkdtemp(prefix="tracker-start-boundary-")
os.environ["BHR_ORG_TRACKER_HOME"] = _bhome
_serve(200, _directory(range(1, 101)))
_main("--run", "--day", "2026-09-06")
os.environ["BHR_ALLOW_SHRINK"] = "1"
_serve(200, _directory(range(1, 41)))
_main("--run", "--day", "2026-09-07")
os.environ.pop("BHR_ALLOW_SHRINK")
for _day, _n in (("2026-09-10", 30), ("2026-09-13", 15)):
    _serve(200, _directory(range(1, _n + 1)))
    _main("--run", "--day", _day)
_rc = _main("--rollup", "weekly", "--day", "2026-09-14")
check("accepted drop: marker on the START snapshot does not excuse a later decline",
      _rc == 1, repr(_rc))

# An accepted drop that reverted the next day cannot excuse a later >50%
# decline nobody accepted (here gradual: each daily step under the guard).
_rhome = tempfile.mkdtemp(prefix="tracker-reverted-")
os.environ["BHR_ORG_TRACKER_HOME"] = _rhome
_serve(200, _directory(range(1, 101)))
_main("--run", "--day", "2026-08-31")
os.environ["BHR_ALLOW_SHRINK"] = "1"
_serve(200, _directory(range(1, 46)))
_main("--run", "--day", "2026-09-05")                          # accepted 100 -> 45
os.environ.pop("BHR_ALLOW_SHRINK")
for _day, _n in (("2026-09-06", 100), ("2026-09-12", 70), ("2026-09-20", 50), ("2026-09-30", 40)):
    _serve(200, _directory(range(1, _n + 1)))
    _main("--run", "--day", _day)
_rc = _main("--rollup", "monthly", "--day", "2026-10-01")
check("accepted drop that reverted does not excuse a later un-accepted decline",
      _rc == 1, repr(_rc))

# The accepted-drop marker only matters to roll-ups: on a normal daily run a
# broken marker is a warning, never a lost report. An accepted drop records
# its marker before the snapshot, so a marker failure there leaves no snapshot.
_mhome = tempfile.mkdtemp(prefix="tracker-marker-")
os.environ["BHR_ORG_TRACKER_HOME"] = _mhome
_serve(200, _directory(range(1, 101)))
_main("--run", "--day", "2026-09-04")
os.makedirs(os.path.join(T._paths()["snapshots"], "accepted-drops.txt"))   # unreadable marker
_serve(200, _directory(range(1, 96)))
_rc = _main("--run", "--day", "2026-09-05")
check("broken marker on a normal run: the run still succeeds and writes its report",
      _rc == 0 and _report_kinds("2026-09-05") == ["departed"] * 5, f"{_rc!r} {_report_kinds('2026-09-05')}")
os.environ["BHR_ALLOW_SHRINK"] = "1"
_serve(200, _directory(range(1, 41)))
_rc = _main("--run", "--day", "2026-09-06")
os.environ.pop("BHR_ALLOW_SHRINK")
check("broken marker on an accepted drop: run fails BEFORE saving a snapshot",
      _rc == 1 and not os.path.exists(T.snapshot_path("2026-09-06")), repr(_rc))
T.Session._open = _real_open

# ------------------------------------------------ the failure email is truthful
print("\n[failure email states what is on disk]")
os.environ.update({"BHR_SMTP_USER": "steve@example.com", "BHR_SMTP_PASSWORD": SECRET,
                   "BHR_MAIL_TO": "steve@example.com"})


def _fail_mail(*argv):
    _inst = with_fake(465, lambda: _main(*argv))
    return " ".join(_inst[0].sent[0].get_content().split()) if _inst and _inst[0].sent else ""


_fhome = tempfile.mkdtemp(prefix="tracker-failmail-")
os.environ["BHR_ORG_TRACKER_HOME"] = _fhome
_serve(200, _directory(range(1, 101)))
_main("--run", "--day", "2026-10-01")
_real_write_report = T.write_report


def _report_fails(rows, day):
    raise T.TrackerError("could not write the report: disk full")


T.write_report = _report_fails
_serve(200, _directory(range(1, 96)))
try:
    _txt = _fail_mail("--run", "--email", "--day", "2026-10-02")    # snapshot saved, then fail
finally:
    T.write_report = _real_write_report
check("report-step failure: the mail says today's changes will not be re-reported "
      "and names --diff-only to recover them",
      "--diff-only" in _txt and "will try again" not in _txt and "NOT report" in _txt, _txt[-160:])
_serve(200, _directory(range(1, 11)))
_txt = _fail_mail("--run", "--email", "--day", "2026-10-02")        # refused same-day rerun
check("refused rerun: the mail says this run's roster was NOT saved and the earlier "
      "snapshot (95 people) stands",
      "NOT saved" in _txt and "95 people" in _txt and "the roster was saved" not in _txt
      and "This run saved" not in _txt, _txt[-160:])
_txt = _fail_mail("--run", "--email", "--day", "2026-10-03")        # refused: none on disk
check("failure email on a day with no snapshot says so",
      "No snapshot for 2026-10-03 is on disk" in _txt and "nothing was checked" in _txt, _txt[-120:])

# the daily note counts people missing a department OR a job title, and says so
_serve(200, json.dumps({"employees": [{"id": i, "displayName": f"P{i}", "department": "D",
                                       "jobTitle": "" if i == 1 else "J"} for i in range(1, 96)]
                        }).encode())
_inst = with_fake(465, lambda: _main("--run", "--email", "--day", "2026-10-04"))
_body = (_inst[0].sent[0].get_body(preferencelist=("plain",)).get_content()
         if _inst and _inst[0].sent else "")
check("incomplete-record note says 'department or job title'",
      "1 record(s) lack a department or job title" in _body, _body[-120:])

# ------------------------------------------ the API's reason survives the fallback
print("\n[API failure: the real reason, not --root]")
_ahome = tempfile.mkdtemp(prefix="tracker-api-")
os.environ["BHR_ORG_TRACKER_HOME"] = _ahome
try:
    for _label, _status, _body, _want in (
            ("401", 401, b"", "(401)"),
            ("403", 403, b"", "hidden from your account"),
            ("empty directory", 200, b'{"employees": []}', "hidden from your account")):
        _serve(_status, _body)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                T.acquire_roster(T.session_from_env(), None)
            _msg = "(no raise)"
        except T.TrackerError as exc:
            _msg = str(exc)
        check(f"API {_label}, no web fallback: error carries the API reason",
              _want in _msg and "--root is required" not in _msg, _msg[:90])
        check(f"API {_label}: error names a next step",
              "Next step" in _msg or "admin" in _msg, _msg[-60:])
    check("403 / empty directory advice is plain language and points at the admin",
          "admin" in _msg and "API access" in _msg, _msg[:90])
    _serve(401, b"")
    _inst = with_fake(465, lambda: _main("--run", "--email", "--day", "2026-09-04"))
    _fm = _inst[0].sent[0].get_content() if _inst and _inst[0].sent else ""
    check("failure email after a 401 names the 401, not --root",
          "(401)" in _fm and "--root" not in _fm, _fm.splitlines()[2][:80] if _fm else "no mail")

    # the web fallback still runs when it IS configured
    def _api_down_web_up(self, url, headers):
        if "api.bamboohr.com" in url:
            return 401, b"", ""
        return 200, b"PersonID,Name,SupervisorID\n1,A,\n2,B,1\n", "text/csv"
    T.Session._open = _api_down_web_up
    os.environ["BHR_SESSION_COOKIE"] = "offline-fake-cookie"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _ppl, _src = T.acquire_roster(T.session_from_env(), "1")
        check("API down + cookie and --root set: the web fallback still runs",
              _src == "web" and len(_ppl) == 2, _src)
    except T.TrackerError as exc:
        check("API down + cookie and --root set: the web fallback still runs", False, str(exc)[:80])
    finally:
        os.environ.pop("BHR_SESSION_COOKIE", None)

    def _api_and_web_down(self, url, headers):
        if "api.bamboohr.com" in url:
            return 401, b"", ""
        return 503, b"", "text/plain"
    T.Session._open = _api_and_web_down
    os.environ["BHR_SESSION_COOKIE"] = "offline-fake-cookie"
    try:
        T.acquire_roster(T.session_from_env(), "1")
        _msg = "(no raise)"
    except T.TrackerError as exc:
        _msg = str(exc)
    check("API and configured web fallback both fail: error contains both reasons",
          "(401)" in _msg and "HTTP 503" in _msg, _msg[:150])
    os.environ.pop("BHR_SESSION_COOKIE", None)

    try:
        T.acquire_roster(T.Session("acme", None, None), None)
        _msg = "(no raise)"
    except T.TrackerError as exc:
        _msg = str(exc)
    check("no API key or complete web fallback: error says no roster source is configured",
          "No roster source is configured" in _msg and "BHR_PERSONAL_API_KEY is not set" in _msg
          and "BHR_SESSION_COOKIE" in _msg and "Next step" in _msg
          and "--root is required" not in _msg, _msg[:150])

    # an unexpected JSON shape is a TrackerError (failure mail, notification),
    # never a raw exception that skips them
    for _label, _body in (("JSON list", b"[1, 2]"), ("non-object entries", b'{"employees": ["x"]}'),
                          ("invalid JSON", b"<html>"), ("employees not a list", b'{"employees": 5}'),
                          ("deeply nested JSON", b"[" * 100000 + b"]" * 100000)):
        _serve(200, _body)
        try:
            T.fetch_roster_api(T.session_from_env())
            _got = "no raise"
        except T.TrackerError:
            _got = "TrackerError"
        except Exception as exc:  # noqa: BLE001
            _got = type(exc).__name__
        check(f"API {_label}: raises TrackerError", _got == "TrackerError", _got)
    _serve(200, b'{"employees": [{"displayName": "A"}, {"department": "D"}]}')
    try:
        T.fetch_roster_api(T.session_from_env())
        _got = "no raise"
    except T.TrackerError as exc:
        _got = str(exc)
    except Exception as exc:  # noqa: BLE001
        _got = type(exc).__name__
    check("API directory entries all lack id: raises contextual TrackerError",
          "unexpected shape" in _got and "id" in _got, _got)
    _inst = with_fake(465, lambda: _main("--run", "--email", "--day", "2026-09-05"))
    check("API entries all lack id: --run --email sends the failure email",
          _inst and _inst[0].sent and "FAILED" in _inst[0].sent[0]["Subject"]
          and "unexpected shape" in _inst[0].sent[0].get_content(),
          f"{len(_inst)} SMTP session(s)")
    _serve(200, b"[1, 2]")
    _inst = with_fake(465, lambda: _main("--run", "--email", "--day", "2026-09-04"))
    check("API returns a JSON list: run exits 1 and the failure email is sent",
          _inst and _inst[0].sent and "FAILED" in _inst[0].sent[0]["Subject"],
          f"{len(_inst)} SMTP session(s)")
finally:
    T.Session._open = _real_open
    T.wait_for_network = _real_wait

# -------------------------------------------- installer without systemd (copy only)
print("\n[install_systemd.sh without systemd -- run on a temp copy, never in place]")
_ihome = tempfile.mkdtemp(prefix="tracker-installer-")
_HERE = os.path.dirname(os.path.abspath(__file__))


def _installer(oncal=None, systemctl=False, shell="/bin/sh", repo_dir_name="repo"):
    """Run a COPY of install_systemd.sh with a temp HOME and a PATH holding
    only the tools it needs -- plus, optionally, recording systemctl/loginctl
    stubs. The real installer, systemctl and the real HOME are never touched."""
    work = tempfile.mkdtemp(dir=_ihome)
    for d in (repo_dir_name, "home", "bin", "data"):
        os.makedirs(os.path.join(work, d))
    shutil.copy(os.path.join(_HERE, "install_systemd.sh"), os.path.join(work, repo_dir_name))
    for tool in ("python3", "dirname", "chmod", "mkdir", "cat", "sed", "rm"):
        if shutil.which(tool):
            os.symlink(shutil.which(tool), os.path.join(work, "bin", tool))
    if systemctl:
        for stub in ("systemctl", "loginctl"):
            p = os.path.join(work, "bin", stub)
            with open(p, "w") as fh:
                fh.write(f'#!/bin/sh\necho "{stub} $*" >> "{work}/calls"\n')
            os.chmod(p, 0o755)
    secrets = os.path.join(work, "home", "secrets.env")
    open(secrets, "w").close()
    env = {"HOME": os.path.join(work, "home"), "PATH": os.path.join(work, "bin"),
           "BHR_SUBDOMAIN": "acme", "BHR_SECRETS_FILE": secrets, "USER": "tester",
           "BHR_ORG_TRACKER_HOME": os.path.join(work, "data")}
    if oncal is not None:
        env["BHR_TRACKER_ONCALENDAR"] = oncal
    p = subprocess.run([shell, os.path.join(work, repo_dir_name, "install_systemd.sh")],
                       env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, work


def _cron_line(out):
    return next((ln.strip() for ln in out.splitlines() if " * * * " in ln), "")


def _cron_words(line):
    """The crontab command as the shell cron runs would split it; [] when
    there is no line or its quoting is broken."""
    try:
        return shlex.split(line.split(" * * * ", 1)[1]) if line else []
    except ValueError:
        return []


_rc, _out, _w = _installer()
_line = _cron_line(_out)
_runner = os.path.join(_w, "repo", "run_daily.sh")
check("no systemd: installer exits 0 after doing its job", _rc == 0, f"rc={_rc}")
check("no systemd: run_daily.sh is generated before the crontab line is printed",
      os.path.exists(_runner) and _runner in _line, _line[:80])
check("no systemd: default schedule is the configured 09:05, not 07:15",
      _line.startswith("5 9 * * * ") and "15 7 * * *" not in _out, _line[:40])
_words = _cron_words(_line)
check("no systemd: the crontab command runs the generated runner and appends to the log",
      _words[:2] == ["/bin/sh", _runner] and _words[2:3] == [">>"]
      and _words[3:4] == [os.path.join(_w, "data", "tracker.log")], str(_words)[:90])
_rc, _out, _w = _installer("*-*-* 17:30:00")
check("no systemd: a simple OnCalendar time becomes the crontab time",
      _rc == 0 and _cron_line(_out).startswith("30 17 * * * "), _cron_line(_out)[:30])
_rc, _out, _w = _installer("*-*-* 7:05")
check("no systemd: single-digit hour accepted", _cron_line(_out).startswith("5 7 * * * "),
      _cron_line(_out)[:30])
_rc, _out, _w = _installer("Mon *-*-* 06:00")
check("no systemd: a complex OnCalendar falls back to 09:05 and says so",
      _rc == 0 and _cron_line(_out).startswith("5 9 * * * ") and "09:05" in _out, _out[-120:])
_rc, _out, _w = _installer("*-*-* 17:30:00", systemctl=True)
_calls = open(os.path.join(_w, "calls")).read() if os.path.exists(os.path.join(_w, "calls")) else ""
_timer = os.path.join(_w, "home", ".config", "systemd", "user", "bhr-org-tracker.timer")
check("with systemd (stubbed): timer installed as before, no crontab line",
      _rc == 0 and "systemctl --user enable --now bhr-org-tracker.timer" in _calls
      and os.path.exists(_timer) and "OnCalendar=*-*-* 17:30:00" in open(_timer).read()
      and not _cron_line(_out), f"rc={_rc} {_calls[:60]!r}")
_hint = next((ln for ln in _out.splitlines() if ln.startswith("test email:")), "")
check("with systemd: the 'test email' hint runs org_tracker.py --test-email, not the daily runner",
      "org_tracker.py" in _hint and "--test-email" in _hint and "run_daily.sh" not in _hint, _hint)
_rc, _out, _w = _installer(systemctl=True, repo_dir_name="my tools")
_svc = os.path.join(_w, "home", ".config", "systemd", "user", "bhr-org-tracker.service")
_exec = next((ln for ln in open(_svc).read().splitlines() if ln.startswith("ExecStart=")), "") \
    if os.path.exists(_svc) else ""
check("with systemd: a project path with a space is quoted in ExecStart",
      _exec == f'ExecStart=/bin/sh "{os.path.join(_w, "my tools", "run_daily.sh")}"', _exec)
if os.path.exists("/bin/dash"):
    # '%' must reach cron escaped, a quote must survive the quoting, and a
    # backslash must not be eaten by dash's escape-interpreting echo
    _odd = "repo%'quoted\\tdir"
    _rc, _out, _w = _installer(shell="/bin/dash", repo_dir_name=_odd)
    _line = _cron_line(_out)
    _words = _cron_words(_line)
    _runner = os.path.join(_w, _odd, "run_daily.sh")
    check("no systemd: under dash, a runner path with %, ' and \\ survives the crontab line",
          _rc == 0 and _words[:2] == ["/bin/sh", _runner.replace("%", "\\%")],
          repr(_words[:2]))
else:
    print("  SKIP  dash cron quoting check (/bin/dash absent)")
shutil.rmtree(_ihome, ignore_errors=True)

# ------------------------------------------------------------- .env.example
print("\n[.env.example]")
_example = os.path.join(_HERE, ".env.example")
_etext = open(_example, encoding="utf-8").read()
check(".env.example: attach option described as the change list, not the full roster",
      "full roster CSV" not in _etext and "attach the change list" in _etext)
_p = subprocess.run(["/bin/sh", "-c", '. "$1"; printf "%s\\0%s\\0%s" "${BHR_SMTP_USER-}" '
                     '"${BHR_SMTP_PASSWORD-}" "${BHR_MAIL_TO-}"', "sh", _example],
                    capture_output=True, env={"PATH": "/usr/bin:/bin"})
_vals = dict(zip(("BHR_SMTP_USER", "BHR_SMTP_PASSWORD", "BHR_MAIL_TO"),
                 _p.stdout.decode().split("\0")))
for _k in _vals:
    os.environ.pop(_k, None)
os.environ.update({k: v for k, v in _vals.items() if v})
check(".env.example copied as-is leaves email unconfigured (no placeholder SMTP)",
      _p.returncode == 0 and not T.MailConfig().configured, str({k: bool(v) for k, v in _vals.items()}))
check(".env.example says how to turn email on", "uncomment" in _etext.lower())

for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
for _d in (_ghome, _uhome, _shome, _bhome, _ahome, _rhome, _mhome, _fhome):
    shutil.rmtree(_d, ignore_errors=True)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
