#!/usr/bin/env python3
"""Offline checks for onboard.py:  python3 test_onboard.py

Throwaway HOME, data folder and settings files; scripted answers; stubbed HTTP; child
processes recorded, never run. No credentials, network, installer, brew or email."""
import ast, contextlib, io, json, os, shutil, stat, subprocess, sys, tempfile, types
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="onboard-test-")
os.environ["HOME"] = os.path.join(TMP, "home")
os.makedirs(os.environ["HOME"])
for k in [k for k in os.environ if k.startswith("BHR_")]:
    del os.environ[k]
os.environ["BHR_ORG_TRACKER_HOME"] = os.path.join(TMP, "data")   # never the repo's data dirs

import onboard as O
import org_tracker as T

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


files_under = lambda root: sorted(os.path.join(d, n) for d, _, names in os.walk(root) for n in names)
mode = lambda path: stat.S_IMODE(os.stat(path).st_mode)


def sh_value(path, key):
    """What /bin/sh sees after sourcing the file -- the daily runner's view."""
    p = subprocess.run(["/bin/sh", "-c", f'. "$1"; printf %s "${key}"', "sh", path],
                       capture_output=True, env={"PATH": "/usr/bin:/bin"})
    return p.stdout.decode("utf-8", "surrogateescape"), p.returncode


def read_settings(path):
    """(text, plain values, expression keys), as onboard reads them."""
    entries = O.parse_settings(open(path, newline="").read())
    return ("".join(e["raw"] for e in entries), {e["key"]: e["value"] for e in entries
            if e["key"] and not e["expr"]}, {e["key"] for e in entries if e["expr"]})


def write(path, text, perms=0o600):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, perms)


print("\n[merge keeps what it doesn't manage]")
foreign = ["# my secrets\n", "export OTHER_TOKEN='abc def'  # note\n", "\n",
           "export BHR_SESSION_COOKIE='c=1; d=2'\n", "# export BHR_SMTP_HOST=smtp.fastmail.com\n",
           "PATH_EXTRA=/opt/x\n"]
existing = ("export BHR_SUBDOMAIN=old\n" + "".join(foreign[:3]) + "export BHR_PERSONAL_API_KEY=oldkey\n"
            + "".join(foreign[3:]) + "BHR_SUBDOMAIN=older\n")
updates = {"BHR_SUBDOMAIN": "acme", "BHR_SMTP_USER": "me@example.com"}
merged = O.merge_settings(existing, updates)
kept = [e["raw"] for e in O.parse_settings(merged) if e["key"] is None and e["raw"] != O.HEADER]
check("every foreign line kept, in order", kept[:len(foreign)] == foreign, repr(kept))
keys = [e["key"] for e in O.parse_settings(merged) if e["key"]]
check("no managed key defined twice", len(keys) == len(set(keys)), str(keys))
check("updated key replaced in place", merged.startswith("export BHR_SUBDOMAIN=acme\n"))
check("untouched managed key kept verbatim", "export BHR_PERSONAL_API_KEY=oldkey\n" in merged)
check("new key appended once under the header", merged.count("BHR_SMTP_USER=") == merged.count(O.HEADER) == 1)
check("merge is idempotent", O.merge_settings(merged, updates) == merged)
check("duplicates collapse to the effective (last) value",
      O.merge_settings("export BHR_MAIL_TO=a@x.com\nBHR_MAIL_TO=b@x.com\n", {}) == "BHR_MAIL_TO=b@x.com\n")
check("file without final newline gets a clean block", O.merge_settings("export A=1", {"BHR_SUBDOMAIN": "x"})
      == "export A=1\n\n" + O.HEADER + "export BHR_SUBDOMAIN=x\n")
hand = os.path.join(TMP, "hand.env")
write(hand, "export BHR_SMTP_PASSWORD=ab#cd12345678\nexport BHR_MAIL_TO='a b'\"c\"   # don't\n")
_, plain, exprs = read_settings(hand)
check("hand-written values parse the way sh reads them",
      all(plain[k] == sh_value(hand, k)[0] for k in plain) and not exprs, json.dumps(plain))

print("\n[hostile values round-trip through /bin/sh]")
CANARY = os.path.join(TMP, "canary")
hostile = {
    "BHR_PERSONAL_API_KEY": f"it's a \"key\" with $HOME and `touch {CANARY}` and $(touch {CANARY})",
    "BHR_SMTP_PASSWORD": "line1\nline2\n  indented\ttab",
    "BHR_MAIL_TO": "a b  c;rm -rf /nonexistent;\\back\\slash",
    "BHR_SUBDOMAIN": "'",
    "BHR_SMTP_HOST": "trailing newline\n",
    "BHR_SMTP_PORT": "!hist ~tilde *glob ${UNSET:-dflt} é",
}
qpath = os.path.join(TMP, "hostile.env")
O.write_settings(qpath, O.merge_settings("export KEEP=me\n", hostile))
bad = [k for k, v in hostile.items() if sh_value(qpath, k) != (v, 0)]
check("sh reads every value back byte-exact, nothing executed", not bad and not os.path.exists(CANARY), str(bad))
qtext, plain, exprs = read_settings(qpath)
multi = {k for k, v in hostile.items() if "\n" in v}   # a quote open past its line is kept as written
check("onboard reads single-line values back as plain values, multi-line ones as kept-as-written",
      plain == {k: v for k, v in hostile.items() if k not in multi} and exprs == multi, str(exprs))
check("re-merging hostile values is idempotent", O.merge_settings(qtext, hostile) == qtext)

print("\n[settings file writes]")
wdir = os.path.join(TMP, "w")
os.makedirs(wdir)
fresh = os.path.join(wdir, "fresh.env")
check("new file: no backup, mode 0600", O.write_settings(fresh, "export A=1\n") is None and mode(fresh) == 0o600)
old_umask = os.umask(0o777)
O.write_settings(os.path.join(wdir, "umask.env"), "export A=1\n")
os.umask(old_umask)
check("exact 0600 even under a 0777 umask", mode(os.path.join(wdir, "umask.env")) == 0o600)
loose = os.path.join(wdir, "loose.env")
write(loose, "export OLD=1\n", 0o644)
bk = O.write_settings(loose, "export NEW=1\n")
check("existing 0644 file becomes 0600; backup <path>.bak is 0600 with the old content",
      mode(loose) == 0o600 and bk == loose + ".bak" and mode(bk) == 0o600 and open(bk).read() == "export OLD=1\n")
O.write_settings(loose, "export NEWER=1\n")
check("one backup only, holding the previous version", open(bk).read() == "export NEW=1\n"
      and sorted(f for f in os.listdir(wdir) if f.startswith("loose")) == ["loose.env", "loose.env.bak"])
target, link = os.path.join(wdir, "target.env"), os.path.join(wdir, "link.env")
write(target, "export X=1\n", 0o644)
os.symlink(target, link)
O.write_settings(link, "export X=2\n")
check("symlink: target written at 0600, link kept",
      os.path.islink(link) and open(target).read() == "export X=2\n" and mode(target) == 0o600)

print("\n[lines set by a shell expression are kept as written]")
EXPR_CANARY = os.path.join(TMP, "expr-canary")
expr_text = (f'export BHR_PERSONAL_API_KEY="$(touch {EXPR_CANARY}; echo k)"\n'
             "export BHR_SUBDOMAIN=acme\nexport BHR_SMTP_HOST=`echo h`\n")
epath = os.path.join(TMP, "expr.env")
write(epath, expr_text)
_, plain, exprs = read_settings(epath)
check("$(...) and backtick lines detected", exprs == {"BHR_PERSONAL_API_KEY", "BHR_SMTP_HOST"}, str(exprs))
m = O.merge_settings(expr_text, {"BHR_PERSONAL_API_KEY": "new", "BHR_SMTP_HOST": "new"})
check("merge leaves expression lines untouched, adds no duplicate", m == expr_text, repr(m))

# ---------------------------------------------------------------- walkthrough harness
CALLS = []


class Script:
    """Answers questions in order; fails loudly on an unexpected one."""

    def __init__(self, answers, secrets=()):
        self.answers, self.secrets, self.prompts = list(answers), list(secrets), []
        self.input = lambda prompt: self._next(self.answers, prompt)
        self.getpass = lambda prompt: self._next(self.secrets, prompt)

    def _next(self, queue, prompt):
        self.prompts.append(prompt)
        if not queue:
            raise AssertionError(f"unexpected question: {prompt!r}")
        a = queue.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


DIRECTORY = json.dumps({"employees": [{"id": 1, "displayName": "Zelda Example", "department": "Eng"},
                                      {"id": 2, "displayName": "Yusuf Example", "department": ""}]}).encode()
FAKE_TRACKER = os.path.join(TMP, "prog", "org_tracker.py")   # what recorded argv names
os.makedirs(os.path.dirname(FAKE_TRACKER))
write(FAKE_TRACKER, "# stand-in; never executed\n", 0o644)


def walk(script, dry, path, platform="linux", http=(200, DIRECTORY, "application/json"), which=()):
    global CALLS
    CALLS = []
    saved = (O.getpass, O.run_child, O.PLATFORM, O.shutil.which, O.TRACKER, T.Session._open)

    def run(argv, env=None, capture=False):
        CALLS.append({"argv": list(argv), "env": dict(env) if env else None})
        return 0, ""
    O.input, O.getpass = script.input, types.SimpleNamespace(getpass=script.getpass)
    O.run_child, O.PLATFORM, O.TRACKER = run, platform, FAKE_TRACKER
    O.shutil.which = lambda name, *a, **kw: "/usr/bin/" + name if name in which else None
    T.Session._open = lambda self, url, headers: http
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = O.main(["--dry-run", "--secrets-file", path] if dry else ["--secrets-file", path])
    except AssertionError as exc:
        rc = f"script: {exc}"
    finally:
        del O.input
        O.getpass, O.run_child, O.PLATFORM, O.shutil.which, O.TRACKER, T.Session._open = saved
    return rc, out.getvalue()


sys.stdin.isatty = lambda: True     # main()'s terminal check; process-level test covers the real one
API = "KEY-SECRET-abcdefghijklmnop-1234"
PW = "smtp-secret-pw-0123456789"
spath = os.path.join(os.environ["HOME"], ".secrets.env")

print("\n[dry run: every question, nothing written or run]")
before = files_under(TMP)
s = Script(["y", "https://Acme.bamboohr.com/",
            "y", "1", "me@example.com", "", "y",   # email: Gmail, from, to=default, test
            "y", "y", "19:30"], [API, "abcd efgh ijkl mnop"])
rc, out = walk(s, True, spath, platform="darwin")
check("dry run completes, wrote no file, started no child process", rc == 0 and not s.answers
      and files_under(TMP) == before and CALLS == [], f"{rc} {set(files_under(TMP)) - set(before)}")
flat = " ".join(out.split())
check("dry run says what it would do", "[dry run] would write " + spath in flat
      and "would send a test email" in flat and "install_cron.sh" in flat)
check("live check shows counts only", "can see 2 people" in out and "Example" not in out)
check("secrets never printed", API not in out and "abcd" not in out and "****" + API[-4:] in out)

print("\n[real run: merge, backup, children get secrets via env only]")
original = "# other tools\nexport GITHUB_TOKEN='ghp_keepme'\nexport BHR_SUBDOMAIN=acme\n"
write(spath, original, 0o644)
s = Script(["y", "",                                  # continue; company default
            "y", "5", "mail.example.org", "587", "me@example.org", "a@example.com, b@example.com",
            "y",                                      # send test email
            "y", "y", "y", ""], [API, PW])            # save, first run, schedule at 09:05
rc, out = walk(s, False, spath)
check("real run completes", rc == 0, str(rc))
txt = open(spath).read()
check("file 0600, foreign lines kept, subdomain line verbatim",
      mode(spath) == 0o600 and txt.startswith(original), repr(txt[:80]))
check("backup made first and its rm command shown",
      open(spath + ".bak").read() == original and "rm " + spath + ".bak" in out)
want = {"BHR_PERSONAL_API_KEY": API, "BHR_SMTP_PASSWORD": PW, "BHR_MAIL_TO": "a@example.com,b@example.com"}
check("sh sees the saved values", all(sh_value(spath, k)[0] == v for k, v in want.items()))
argvs = [" ".join(c["argv"]) for c in CALLS]
check("no secret on any child's command line", not any(API in a or PW in a for a in argvs), str(argvs))
check("secrets never printed", API not in out and PW not in out)
te = [c for c in CALLS if "--test-email" in c["argv"]]
check("test email: org_tracker --test-email, password in env, scratch data dir removed",
      len(te) == 1 and te[0]["env"]["BHR_SMTP_PASSWORD"] == PW
      and not os.path.exists(te[0]["env"]["BHR_ORG_TRACKER_HOME"]))
fr = [c for c in CALLS if c["argv"][:2] == ["/bin/sh", "-c"]]
check("first run sources the settings file", len(fr) == 1 and fr[0]["argv"][4] == spath)
inst = [c for c in CALLS if c["argv"] == ["/bin/sh", O.INSTALLERS["linux"]]]
ienv = inst[0]["env"] if inst else {}
check("installer env: subdomain, secrets file, schedule, data home -- no secrets",
      ienv.get("BHR_SUBDOMAIN") == "acme" and ienv.get("BHR_SECRETS_FILE") == spath
      and ienv.get("BHR_TRACKER_ONCALENDAR") == "*-*-* 09:05:00"
      and ienv.get("BHR_ORG_TRACKER_HOME") == os.environ["BHR_ORG_TRACKER_HOME"]
      and API not in ienv.values() and PW not in ienv.values(), str(sorted(k for k in ienv if "BHR" in k)))
check("Linux without systemctl: summary says NOT scheduled, no 'remove' command offered",
      "NOT scheduled yet" in out and "install_systemd.sh remove" not in out)

print("\n[re-run offers saved values; unchanged file is left alone]")
after = open(spath).read()
s = Script(["y", "", "n", "n", "n"], [""])
rc, out = walk(s, False, spath)
check("re-run completes", rc == 0, str(rc))
check("company offered as default, key masked",
      any("[acme]" in p for p in s.prompts) and any("****" + API[-4:] in p and API not in p for p in s.prompts))
check("unchanged file untouched, backup unchanged",
      open(spath).read() == after and open(spath + ".bak").read() == original)

# -- shell-expression lines in a walkthrough
s = Script(["y", "", "n", "n", "n"])                  # no getpass: the key line is kept
rc, out = walk(s, False, epath)
check("expression key is not asked for and is reported 'kept as written'",
      rc == 0 and "kept as written" in out, str(rc))
check("an email expression line with the rest unset: email reported NOT set up, not 'kept'",
      "Email:          NOT set up -- add BHR_SMTP_USER, BHR_SMTP_PASSWORD, BHR_MAIL_TO" in out)
check("expression file unchanged and never evaluated",
      open(epath).read() == expr_text and not os.path.exists(EXPR_CANARY))

# -- macOS installer env
os.environ["BHR_SESSION_COOKIE"] = "cookie-must-not-reach-installer"
rc, out = walk(Script(["y", "acme", "n", "y", "n", "y", "9:05"], [API]), False, os.path.join(TMP, "mac.env"), "darwin")
del os.environ["BHR_SESSION_COOKIE"]
inst = [c for c in CALLS if c["argv"] == ["/bin/sh", O.INSTALLERS["darwin"]]]
ienv = inst[0]["env"] if inst else {}
check("macOS installer gets plain-integer hour/minute and no inherited BHR secrets; 'remove' offered",
      ienv.get("BHR_TRACKER_HOUR") == "9" and ienv.get("BHR_TRACKER_MINUTE") == "5"
      and "BHR_SESSION_COOKIE" not in ienv and "install_cron.sh remove" in out, str(rc))

print("\n[live check failures]")
s = Script(["y", "acme", "n", "n", "n"], [API])       # no retry; no email; save: no
rc, out = walk(s, False, os.path.join(TMP, "401.env"), http=(401, b"", ""))
check("401: org_tracker's message shown, key not", "API key rejected (401)" in out and API not in out)
check("declining to save writes nothing", rc == 0 and not os.path.exists(os.path.join(TMP, "401.env")))

print("\n[interrupts]")
cpath = os.path.join(TMP, "ctrlc.env")
for exc in (KeyboardInterrupt(), EOFError()):
    before = files_under(TMP)
    rc, out = walk(Script(["y", "acme", "y", "1", exc], [API]), False, cpath)
    check(f"{type(exc).__name__}: exit 130, 'nothing saved', no file",
          rc == 130 and "nothing saved" in out and files_under(TMP) == before, str(rc))

print("\n[process level]")
env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "BHR_ORG_TRACKER_HOME": os.environ["BHR_ORG_TRACKER_HOME"]}
p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "onboard.py"), "--dry-run"],
                   stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env)
check("no TTY on stdin -> exit 2 with a sentence", p.returncode == 2 and "terminal" in p.stderr, str(p.returncode))
tree = ast.parse(open(os.path.join(HERE, "onboard.py")).read(), feature_version=(3, 9))   # SyntaxError: fail
check("parses as Python 3.9", True)
mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
if hasattr(sys, "stdlib_module_names"):
    check("stdlib imports only (plus org_tracker)", mods - set(sys.stdlib_module_names) <= {"org_tracker"})

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
