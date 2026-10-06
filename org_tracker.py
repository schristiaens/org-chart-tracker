#!/usr/bin/env python3
"""bhr-org-tracker — automated BambooHR org-chart snapshot and change report.

Pulls an id-bearing roster, computes the fewest <=6-level org-chart exports that
cover every employee, fetches and parses those exports for job title /
department / division, writes a dated snapshot, and diffs it against the most
recent prior snapshot to report new hires and departures.

Python 3 standard library only. External binary dependency: `pdftotext`
(poppler) for the PDF attribute path -- checked in preflight, and unnecessary
when the CSV attribute path is available.

Credentials come from the environment and are sent as headers, never in a URL:

    BHR_SUBDOMAIN          required   e.g. "acme" for acme.bamboohr.com
    BHR_SESSION_COOKIE     web path   full Cookie header value from a logged-in session
    BHR_PERSONAL_API_KEY   api path   BambooHR personal API key

Usage:
    org_tracker.py --plan                  show the export plan, fetch nothing
    org_tracker.py --check-auth            probe credentials, mutate nothing
    org_tracker.py --run                   full pull, snapshot, diff, report
    org_tracker.py --diff-only             re-diff the two newest snapshots
    org_tracker.py --plan-from FILE.csv    plan against a roster CSV on disk
    org_tracker.py --parse-pdf FILE.pdf    parse one export, print records
    org_tracker.py --rollup auto           produce any weekly/monthly roll-up now due
    org_tracker.py --rollup weekly         this week's roll-up (Monday-anchored)
    org_tracker.py --rollup monthly        previous calendar month's roll-up
"""

from __future__ import annotations

import argparse
import base64
import collections
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

MAX_LEVELS = 6          # endpoint ceiling: an export renders at most 6 levels
HTTP_TIMEOUT = 90
# launchd defers a missed StartCalendarInterval to the next wake, so the daily
# run routinely starts seconds after the lid opens -- before wifi or the VPN
# has settled. Both of these exist for that one scenario.
HTTP_RETRIES = 3            # total attempts per request on a transient fault
HTTP_BACKOFF = (2, 8)       # seconds before attempts 2 and 3
NET_WAIT_SECONDS = 120      # how long to wait for the network before giving up
SHRINK_GUARD = 0.5      # refuse a roster < 50% of the prior snapshot
MIN_PARSE_RATIO = 0.6   # refuse a PDF yielding < 60% of expected people
ATTR_GAP_THRESHOLD = 0.05  # crawl for attributes only if >5% of people lack them
USER_AGENT = "bhr-org-tracker/1.0 (+local automation)"

# Any SMTP provider works; override with BHR_SMTP_HOST / BHR_SMTP_PORT.
# 465 is implicit TLS, 587 is STARTTLS; both are supported.
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465

SNAPSHOT_COLUMNS = [
    "PersonID", "Name", "SupervisorID", "SupervisorName",
    "Department", "Division", "Job Title", "Location",
]
REPORT_COLUMNS = [
    "change_type", "person_id", "name", "job_title", "department",
    "division", "supervisor_name", "detected_date",
]

# The six BambooHR *divisions*. Load-bearing: the org-chart export prints
# name / title / department / division as four consecutive lines, so the
# division vocabulary is what delimits one person's card from the next.
# "Vendor Personnel" looks like a division but is a job title -- treating it
# as one splits every contractor's record in half.
DIVISIONS = frozenset({
    "Sales Division", "Tech", "Marketing Division",
    "Customer Experience", "Product", "General & Administrative",
})
UNFILLED_ROLE = "This role is not filled at the moment."
# Plain-language cause for a 403 or an empty directory -- both mean the key
# works but this account may not read the directory.
DIRECTORY_HIDDEN = ("the employee directory is hidden from your account or API access "
                    "is disabled for it -- ask your BambooHR admin.")


def base_dir() -> str:
    return os.environ.get(
        "BHR_ORG_TRACKER_HOME",
        os.path.dirname(os.path.abspath(__file__)),
    )


def _paths():
    b = base_dir()
    return {
        "snapshots": os.path.join(b, "snapshots"),
        "reports": os.path.join(b, "reports"),
        "exports": os.path.join(b, "exports"),
        "log": os.path.join(b, "tracker.log"),
    }


_STDOUT_IS_LOG: bool | None = None


def _stdout_is_log() -> bool:
    """True when stdout already lands in tracker.log.

    launchd points both StandardOutPath and StandardErrorPath at the log file,
    so printing *and* appending wrote every line twice. Comparing the open
    stream to the log by (device, inode) is exact, and it keeps `--run | tee`
    working -- which a plain isatty() check would not.
    """
    global _STDOUT_IS_LOG
    if _STDOUT_IS_LOG is None:
        _STDOUT_IS_LOG = False
        try:
            a = os.fstat(sys.stdout.fileno())
            b = os.stat(_paths()["log"])
            _STDOUT_IS_LOG = (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
        except (OSError, ValueError, AttributeError):
            pass
    return _STDOUT_IS_LOG


def log(msg: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {msg}"
    if not _stdout_is_log():
        print(line)
    try:
        with open(_paths()["log"], "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class TrackerError(RuntimeError):
    """Fatal, actionable condition. Always exits non-zero."""


def _transient(reason: BaseException | str | None) -> bool:
    """Whether a transport fault is worth retrying.

    A resolver or route that is seconds from being ready clears on its own, so
    those retry. A certificate that will not verify is a different animal --
    usually a TLS-intercepting network or a captive portal -- and retrying it
    only delays the fallback to the web roster path.
    """
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    return isinstance(reason, (TimeoutError, ConnectionError, socket.gaierror, OSError))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Session:
    """Authenticated BambooHR client. Two auth paths, one interface.

    A login redirect is the failure mode that matters most: an expired cookie
    returns 302 to the marketing site with a 200-shaped body, which would
    otherwise be parsed as an empty roster and reported as "no changes".
    """

    def __init__(self, subdomain: str, cookie: str | None, api_key: str | None):
        if not subdomain:
            raise TrackerError(
                "BHR_SUBDOMAIN is not set. Needed to build "
                "https://<subdomain>.bamboohr.com/ -- see --check-auth."
            )
        # accept "acme", "acme.bamboohr.com", or a full URL -- the
        # gateway path wants the bare subdomain
        subdomain = subdomain.strip().rstrip("/")
        if "//" in subdomain:
            subdomain = subdomain.split("//", 1)[1]
        subdomain = subdomain.split("/", 1)[0]
        if subdomain.endswith(".bamboohr.com"):
            subdomain = subdomain[: -len(".bamboohr.com")]
        self.subdomain = subdomain
        self.cookie = cookie
        self.api_key = api_key
        self.host = f"https://{subdomain}.bamboohr.com"
        self.api_host = "https://api.bamboohr.com"

    # -- request plumbing --------------------------------------------------

    def _open(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
        req = urllib.request.Request(url, headers=headers, method="GET")

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None  # surface the 302 instead of following it to a login page

        opener = urllib.request.build_opener(_NoRedirect)
        last = f"request to {_redact(url)} failed"
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                    return resp.status, resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                # A real response, not a transport fault. Never retried: the
                # caller turns the status into its own actionable message.
                body = exc.read() if exc.fp else b""
                return exc.code, body, exc.headers.get("Content-Type", "") if exc.headers else ""
            except TimeoutError:
                # The read phase, after the connection is established. This is
                # NOT a URLError; uncaught, it would kill the process with no
                # web fallback, no failure mail and no notification -- a dead
                # run that looks exactly like a run that never happened.
                last = f"timed out after {HTTP_TIMEOUT}s reading {_redact(url)}"
            except urllib.error.URLError as exc:
                last = f"network error for {_redact(url)}: {exc.reason}"
                if not _transient(exc.reason):
                    raise TrackerError(last) from exc
            except OSError as exc:
                last = f"connection error for {_redact(url)}: {exc}"
            if attempt < HTTP_RETRIES:
                pause = HTTP_BACKOFF[attempt - 1]
                log(f"{last} -- retrying in {pause}s (attempt {attempt + 1} of {HTTP_RETRIES})")
                time.sleep(pause)
        raise TrackerError(f"{last} (after {HTTP_RETRIES} attempts)")

    def get_web(self, path: str, params: list[tuple[str, str]] | None = None) -> bytes:
        if not self.cookie:
            raise TrackerError(
                "BHR_SESSION_COOKIE is not set; the web export route needs a "
                "logged-in session cookie."
            )
        url = self.host + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Cookie": self.cookie,
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        }
        status, body, ctype = self._open(url, headers)
        if status in (301, 302, 303, 307, 308):
            raise TrackerError(
                f"session rejected (HTTP {status} redirect) for {_redact(url)} -- "
                "the cookie in BHR_SESSION_COOKIE is expired or invalid. "
                "Refusing to write a snapshot."
            )
        if status == 403:
            raise TrackerError(f"forbidden (403) for {_redact(url)} -- session lacks access.")
        if status != 200:
            raise TrackerError(f"HTTP {status} for {_redact(url)}")
        if b"<title>Log In" in body[:4000] or b"login" in ctype.lower().encode():
            raise TrackerError("response looks like a login page -- treating as auth failure.")
        return body

    def get_api(self, path: str) -> bytes:
        if not self.api_key:
            raise TrackerError("BHR_PERSONAL_API_KEY is not set.")
        url = f"{self.api_host}/api/gateway.php/{self.subdomain}{path}"
        token = base64.b64encode(f"{self.api_key}:x".encode()).decode()
        headers = {
            "Authorization": f"Basic {token}",   # never in the URL
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        status, body, _ = self._open(url, headers)
        if status == 401:
            raise TrackerError(
                f"API key rejected (401) by subdomain '{self.subdomain}'. The subdomain "
                "itself is valid (a wrong one returns 404), so the key is revoked, "
                "belongs to a different BambooHR instance (a sandbox key will 401 "
                "against production), or that account lacks API access. Regenerate it "
                "from BambooHR > your profile > API Keys."
            )
        if status == 404:
            raise TrackerError(
                f"API returned 404 for subdomain '{self.subdomain}' -- wrong subdomain, "
                "or this account lacks API access."
            )
        if status == 403:
            raise TrackerError(f"API returned 403 for subdomain '{self.subdomain}' -- {DIRECTORY_HIDDEN}")
        if status != 200:
            raise TrackerError(f"HTTP {status} from API {path}")
        return body


def _redact(url: str) -> str:
    """Strip anything query-shaped that could carry a secret before logging."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def session_from_env() -> Session:
    return Session(
        subdomain=os.environ.get("BHR_SUBDOMAIN", "").strip(),
        cookie=os.environ.get("BHR_SESSION_COOKIE", "").strip() or None,
        api_key=os.environ.get("BHR_PERSONAL_API_KEY", "").strip() or None,
    )


# --------------------------------------------------------------------------
# Roster: identity + structure
# --------------------------------------------------------------------------

Person = dict  # PersonID, Name, SupervisorID, Department, Division, Job Title, Location


def roster_from_csv_bytes(blob: bytes) -> list[Person]:
    text = blob.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise TrackerError("roster CSV parsed to zero rows.")
    out = []
    for r in rows:
        pid = (_pick(r, "PersonID", "Employee Id", "id", "Employee #") or "").strip()
        if not pid:
            continue
        out.append({
            "PersonID": pid,
            "Name": (_pick(r, "Name", "Employee", "displayName") or "").strip(),
            "SupervisorID": (_pick(r, "SupervisorID", "Supervisor Id", "Manager Id") or "").strip(),
            "Department": (_pick(r, "Department") or "").strip(),
            "Division": (_pick(r, "Division") or "").strip(),
            "Job Title": (_pick(r, "Job Title", "jobTitle", "Title") or "").strip(),
            "Location": (_pick(r, "Location") or "").strip(),
        })
    if not out:
        raise TrackerError("roster CSV had rows but no usable PersonID column.")
    return out


def _pick(row: dict, *names: str):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    # case-insensitive second pass
    low = {k.lower(): v for k, v in row.items() if k}
    for n in names:
        v = low.get(n.lower())
        if v:
            return v
    return None


def fetch_roster_api(sess: Session) -> list[Person]:
    """Preferred path: one request, whole company, departments included."""
    blob = sess.get_api("/v1/employees/directory")
    # Any other shape is a failed pull, and must take the TrackerError path
    # (failure mail, notification) rather than escape as a raw exception.
    try:
        data = json.loads(blob.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):   # RecursionError: absurdly deep nesting
        raise TrackerError("API directory response is not JSON -- wrong subdomain, "
                           "or a proxy answered instead of BambooHR.") from None
    if not isinstance(data, dict) or not isinstance(data.get("employees") or [], list):
        raise TrackerError("API directory response has an unexpected shape -- "
                           "refusing to read it as a roster.")
    employees = data.get("employees") or []
    if not employees:
        raise TrackerError(f"API directory returned no employees -- {DIRECTORY_HIDDEN}")
    if not all(isinstance(e, dict) for e in employees):
        raise TrackerError("API directory entries have an unexpected shape -- "
                           "refusing to read them as a roster.")

    def text(v) -> str:
        return "" if v is None else str(v).strip()

    out = []
    for e in employees:
        out.append({
            "PersonID": text(e.get("id")),
            "Name": text(e.get("displayName")),
            # the directory gives the supervisor's DISPLAY NAME, not an id
            "SupervisorID": "",
            "SupervisorName": text(e.get("supervisor")),
            "Department": text(e.get("department")),
            "Division": text(e.get("division")),
            "Job Title": text(e.get("jobTitle")),
            "Location": text(e.get("location")),
        })
    out = [p for p in out if p["PersonID"]]
    if not out:
        raise TrackerError("API directory entries have an unexpected shape -- "
                           "none contains a usable id.")
    _resolve_supervisor_ids(out)
    return out


def _resolve_supervisor_ids(people: list[Person]) -> int:
    """Best-effort SupervisorName -> SupervisorID, for the tree.

    Only unambiguous names resolve. A duplicated display name leaves the edge
    empty rather than attaching a person to the wrong manager -- an empty edge
    makes them a component root, which the planner already handles, whereas a
    wrong edge would silently corrupt the hierarchy.
    """
    by_name: dict[str, list[str]] = collections.defaultdict(list)
    for p in people:
        by_name[p["Name"]].append(p["PersonID"])
    resolved = 0
    for p in people:
        name = p.get("SupervisorName", "")
        ids = by_name.get(name, [])
        if name and len(ids) == 1 and ids[0] != p["PersonID"]:
            p["SupervisorID"] = ids[0]
            resolved += 1
    return resolved


def fetch_roster_web(sess: Session, root_id: str) -> list[Person]:
    """Org-chart CSV export. Observed returning the whole company from any
    root, so it is not depth-limited the way the PDF is -- but it ships empty
    Department/Job Title unless the same orgChartOptions the PDF route accepts
    also apply here (unverified)."""
    blob = sess.get_web(
        f"/employees/exportable_org_chart/csv/{root_id}",
        chart_params(MAX_LEVELS) + [("type", "csv")],
    )
    return roster_from_csv_bytes(blob)


def chart_params(levels: int) -> list[tuple[str, str]]:
    return [
        ("orgChartOptions[]", "department"),
        ("orgChartOptions[]", "division"),
        ("orgChartOptions[]", "job_title"),
        ("orgChartOptions[]", "name"),
        ("exportCustomizeRadio", "customize"),
        ("selectedLevel", str(levels)),
        ("nodeHeight", "84"),
    ]


# --------------------------------------------------------------------------
# Cover planner -- "the least number of org charts needed"
# --------------------------------------------------------------------------

class Forest:
    """The org as a forest, not a tree: people with no resolvable supervisor
    are roots of their own components, so a single-root crawl misses people."""

    def __init__(self, people: list[Person]):
        self.by_id = {p["PersonID"]: p for p in people}
        self.children: dict[str, list[str]] = collections.defaultdict(list)
        self.parent: dict[str, str | None] = {}
        for p in people:
            pid, sid = p["PersonID"], p["SupervisorID"]
            sid = sid if sid and sid in self.by_id and sid != pid else None
            self.parent[pid] = sid
            if sid:
                self.children[sid].append(pid)
        self.roots = sorted(pid for pid, sid in self.parent.items() if sid is None)
        self._check_cycles()

    def _check_cycles(self) -> None:
        """A supervisor cycle would hang the descent. Detect, don't trust."""
        reachable = set()
        for r in self.roots:
            reachable |= self.descendants(r, None)
        orphaned = set(self.by_id) - reachable
        if orphaned:
            raise TrackerError(
                f"{len(orphaned)} employees unreachable from any root -- "
                f"supervisor cycle suspected (e.g. {sorted(orphaned)[:5]})."
            )

    def descendants(self, root: str, max_levels: int | None) -> set[str]:
        """Nodes within `max_levels` of `root`, root counted as level 1."""
        out = {root}
        frontier = [(root, 1)]
        while frontier:
            node, depth = frontier.pop()
            if max_levels is not None and depth >= max_levels:
                continue
            for kid in self.children.get(node, ()):
                if kid not in out:
                    out.add(kid)
                    frontier.append((kid, depth + 1))
        return out

    def depths(self, root: str) -> dict[str, int]:
        d = {root: 1}
        queue = collections.deque([root])
        while queue:
            node = queue.popleft()
            for kid in self.children.get(node, ()):
                if kid not in d:
                    d[kid] = d[node] + 1
                    queue.append(kid)
        return d


def plan_cover(forest: Forest, max_levels: int = MAX_LEVELS) -> list[str]:
    """Fewest export roots whose <=max_levels subtrees cover every employee.

    Bottom-up: take the DEEPEST uncovered node and lift max_levels-1 ancestors,
    which is the highest root that still reaches it -- and therefore the one
    covering the most other nodes.

    The intuitive top-down greedy ("root at the shallowest uncovered node") is
    NOT optimal here. A level-6 manager with five reports costs one export if
    you root at the manager and five if you root at each report.
    """
    if max_levels < 1:
        raise ValueError("max_levels must be >= 1")
    chosen: list[str] = []
    covered: set[str] = set()
    for component_root in forest.roots:
        depth = forest.depths(component_root)
        for node in sorted(depth, key=lambda n: -depth[n]):
            if node in covered:
                continue
            root, lifted = node, 0
            while lifted < max_levels - 1:
                parent = forest.parent.get(root)
                if not parent or parent not in depth:
                    break
                root, lifted = parent, lifted + 1
            chosen.append(root)
            covered |= forest.descendants(root, max_levels)
    missing = set(forest.by_id) - covered
    if missing:
        raise TrackerError(f"cover plan misses {len(missing)} employees -- planner bug.")
    return chosen


# --------------------------------------------------------------------------
# Export parsing -- attributes
# --------------------------------------------------------------------------

def pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def pdf_to_lines(pdf_path: str) -> list[str]:
    if not pdftotext_available():
        raise TrackerError(
            "`pdftotext` not found (install poppler: brew install poppler). "
            "Needed only for the PDF attribute path."
        )
    proc = subprocess.run(
        ["pdftotext", "-raw", pdf_path, "-"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise TrackerError(f"pdftotext failed on {pdf_path}: {proc.stderr.decode()[:300]}")
    out = []
    for raw in proc.stdout.decode("utf-8", errors="replace").splitlines():
        line = raw.replace(UNFILLED_ROLE, "").strip()
        if line:
            out.append(line)
    return out


def parse_org_pdf(pdf_path: str, expected: int | None = None) -> tuple[list[dict], list[str]]:
    """Parse an org-chart PDF export into person cards.

    Cards print as four consecutive lines -- name, job title, department,
    division -- optionally followed by a direct-report count. The division
    vocabulary is the delimiter. A department may itself be named like a
    division ("Product" under "Product"), so a division line only closes a card
    when the NEXT line is not also a division.

    Returns (cards, repairs) where `repairs` records every non-standard segment
    so a layout change shows up as an audit line instead of silent data loss.
    """
    lines = pdf_to_lines(pdf_path)
    cards: list[dict] = []
    repairs: list[str] = []
    buf: list[str] = []

    for i, line in enumerate(lines):
        # a bare integer after a completed card is that person's report count
        if line.isdigit() and not buf and cards and cards[-1].get("reports") is None:
            cards[-1]["reports"] = int(line)
            continue
        buf.append(line)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line in DIVISIONS and nxt not in DIVISIONS and len(buf) >= 3:
            cards.extend(_emit_segment(buf, repairs))
            buf = []
    if buf:
        repairs.append(f"trailing fragment discarded: {buf!r}")

    if expected is not None and len(cards) < expected * MIN_PARSE_RATIO:
        raise TrackerError(
            f"{os.path.basename(pdf_path)} yielded {len(cards)} people, expected "
            f"~{expected}. Refusing partial data (collapsed or truncated export?)."
        )
    return cards, repairs


def _emit_segment(buf: list[str], repairs: list[str]) -> list[dict]:
    """Turn one buffered segment into 1+ cards.

    The clean case is exactly four lines. Two real deviations occur in live
    exports, both from adjacent cards' text merging into one run:
      6 lines -> two people sharing the trailing department/division
      leading render fragments ("Sales D", "Sa") -> dropped
    """
    def card(name, title, dept, div):
        return {"name": name, "title": title, "dept": dept, "div": div, "reports": None}

    if len(buf) == 4:
        return [card(buf[0], buf[1], buf[2], buf[3])]

    dept, div = buf[-2], buf[-1]
    head = buf[:-2]
    out = []
    # walk the head in name/title pairs, right to left
    while len(head) >= 2:
        name, title = head[-2], head[-1]
        head = head[:-2]
        if len(title) < 3 or len(name) < 3:
            repairs.append(f"dropped render fragment: {name!r}/{title!r}")
            continue
        out.append(card(name, title, dept, div))
    if head:
        repairs.append(f"dropped odd leftover: {head!r}")
    out.reverse()
    if len(buf) != 4:
        repairs.append(f"{len(buf)}-line segment split into {len(out)} card(s): "
                       f"{[c['name'] for c in out]}")
    return out


def merge_attributes(people: list[Person], cards: list[dict],
                     scope_ids: set[str] | None = None) -> int:
    """Attach title/department/division from chart cards onto roster people.

    Joined on display name, scoped to the subtree the export covered, which is
    what keeps a duplicate name from taking the wrong department. Roster values
    already present win -- the API path supplies them directly and needs no chart.
    """
    index: dict[str, list[Person]] = collections.defaultdict(list)
    for p in people:
        if scope_ids is None or p["PersonID"] in scope_ids:
            index[p["Name"]].append(p)
    filled = 0
    for c in cards:
        matches = index.get(c["name"]) or []
        if len(matches) != 1:
            continue  # ambiguous or absent -- leave empty rather than guess
        p = matches[0]
        for key, val in (("Job Title", c["title"]), ("Department", c["dept"]),
                         ("Division", c["div"])):
            if val and not p.get(key):
                p[key] = val
                filled += 1
    return filled


# --------------------------------------------------------------------------
# Snapshots and diff
# --------------------------------------------------------------------------

def snapshot_path(day: str) -> str:
    return os.path.join(_paths()["snapshots"], f"snapshot-{day}.csv")


def report_path(day: str) -> str:
    return os.path.join(_paths()["reports"], f"changes-{day}.csv")


def ensure_dirs() -> None:
    for key in ("snapshots", "reports", "exports"):
        os.makedirs(_paths()[key], mode=0o700, exist_ok=True)


def snapshot_rows(people: list[Person]) -> list[Person]:
    """The rows a snapshot file holds, exactly as read_snapshot returns them.
    The daily diff runs on these BEFORE anything is written, so a roster the
    shrink guard refuses never reaches the disk."""
    by_id = {p["PersonID"]: p for p in people}
    rows = []
    for p in sorted(people, key=lambda r: int(r["PersonID"]) if r["PersonID"].isdigit() else 0):
        sup = by_id.get(p["SupervisorID"])
        row = {c: "" if p.get(c) is None else str(p.get(c)) for c in SNAPSHOT_COLUMNS}
        # prefer the id-resolved name; fall back to whatever the source
        # supplied (the API directory gives a name and no id)
        row["SupervisorName"] = sup["Name"] if sup else p.get("SupervisorName", "")
        rows.append(row)
    return rows


def write_snapshot(people: list[Person], day: str) -> str:
    """Atomic: temp file in the same directory, then rename."""
    ensure_dirs()
    path = snapshot_path(day)
    fd, tmp = tempfile.mkstemp(dir=_paths()["snapshots"], prefix=".snap-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=SNAPSHOT_COLUMNS)
            w.writeheader()
            w.writerows(snapshot_rows(people))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def read_snapshot(path: str) -> list[Person]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def prior_snapshot(day: str) -> str | None:
    ensure_dirs()
    found = []
    for name in os.listdir(_paths()["snapshots"]):
        m = re.fullmatch(r"snapshot-(\d{4}-\d{2}-\d{2})\.csv", name)
        if m and m.group(1) < day:
            found.append((m.group(1), os.path.join(_paths()["snapshots"], name)))
    if not found:
        return None
    return max(found)[1]


def shrank(old_n: int, new_n: int) -> bool:
    """Whether a headcount change trips the shrink guard."""
    return old_n > 0 and new_n < old_n * SHRINK_GUARD


def shrink_allowed() -> bool:
    return os.environ.get("BHR_ALLOW_SHRINK") == "1"


# An accepted drop (BHR_ALLOW_SHRINK=1 on a daily run) is remembered here, one
# "<day> <old> <new>" line per day, so the weekly and monthly roll-ups spanning
# it accept it too instead of refusing every morning until someone hand-runs
# them with the override.
def _accepted_drops_path() -> str:
    return os.path.join(_paths()["snapshots"], "accepted-drops.txt")


def accepted_drops() -> dict[str, tuple[int, int]]:
    """Valid accepted drops keyed by day; malformed lines are ignored."""
    drops = {}
    try:
        with open(_accepted_drops_path(), encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if (len(parts) != 3
                        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0])):
                    continue
                try:
                    dt.date.fromisoformat(parts[0])
                    old_n, new_n = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                if old_n > 0 and new_n >= 0:
                    drops[parts[0]] = (old_n, new_n)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise TrackerError(f"could not read accepted-drop marker: {exc}") from exc
    return drops


def _write_accepted_drops(drops: dict[str, tuple[int, int]]) -> None:
    """Atomically replace the accepted-drop marker, or remove it if empty."""
    ensure_dirs()
    path = _accepted_drops_path()
    if not drops:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise TrackerError(f"could not remove accepted-drop marker: {exc}") from exc
        return
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=_paths()["snapshots"],
                                   prefix=".accepted-drops-", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for day, (old_n, new_n) in sorted(drops.items()):
                fh.write(f"{day} {old_n} {new_n}\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        raise TrackerError(f"could not update accepted-drop marker: {exc}") from exc


def record_accepted_drop(day: str, old_n: int, new_n: int) -> None:
    drops = accepted_drops()
    drops[day] = (old_n, new_n)
    _write_accepted_drops(drops)


def clear_accepted_drop(day: str) -> None:
    """A same-day rerun that no longer trips the guard retracts the day's
    acceptance, so it cannot excuse a later drop nobody accepted."""
    drops = accepted_drops()
    if drops.pop(day, None) is not None:
        _write_accepted_drops(drops)


def accepted_drops_allow(counts: list[tuple[str, int]]) -> bool:
    """Whether accepted drops excuse a shrink between the first and last of
    `counts`, the (day, headcount) of every snapshot in the span.

    The span is cut at each accepted day after the first. Within each piece the
    last headcount must not trip the guard against the piece's peak, so an
    accepted drop excuses only itself: one that later reversed cannot excuse a
    fresh decline nobody accepted.
    """
    accepted = accepted_drops()
    piece: list[int] = []
    for i, (day, n) in enumerate(counts):
        if i and day in accepted:
            if shrank(max(piece), piece[-1]):
                return False
            piece = []
        piece.append(n)
    return bool(piece) and not shrank(max(piece), piece[-1])


def diff_snapshots(old: list[Person], new: list[Person], day: str,
                   allow_shrink: bool = False) -> list[dict]:
    """Keyed on PersonID -- never on display name, which is neither unique nor
    stable. A rename would otherwise read as one departure plus one hire."""
    if not allow_shrink and shrank(len(old), len(new)):
        raise TrackerError(
            f"roster dropped from {len(old)} to {len(new)} (>50%) -- refusing as a "
            "likely truncated pull. Override with BHR_ALLOW_SHRINK=1."
        )
    old_by = {p["PersonID"]: p for p in old}
    new_by = {p["PersonID"]: p for p in new}
    rows = []
    for pid in sorted(set(new_by) - set(old_by), key=_id_sort):
        rows.append(_change_row("new_hire", new_by[pid], new_by, day))
    for pid in sorted(set(old_by) - set(new_by), key=_id_sort):
        # departures carry detail from the snapshot they were last seen in --
        # the whole point of storing Department in the snapshot
        rows.append(_change_row("departed", old_by[pid], old_by, day))
    return rows


def _id_sort(pid: str):
    return (0, int(pid)) if pid.isdigit() else (1, pid)


def _change_row(kind: str, person: Person, cohort: dict[str, Person], day: str) -> dict:
    sup = cohort.get(person.get("SupervisorID", ""))
    return {
        "change_type": kind,
        "person_id": person["PersonID"],
        "name": person.get("Name", ""),
        "job_title": person.get("Job Title", ""),
        "department": person.get("Department", ""),
        "division": person.get("Division", ""),
        "supervisor_name": person.get("SupervisorName") or (sup["Name"] if sup else ""),
        "detected_date": day,
    }


def write_report(rows: list[dict], day: str) -> str:
    return write_rows_csv(rows, report_path(day))


def write_rows_csv(rows: list[dict], path: str) -> str:
    """Atomic CSV write of change rows to an explicit path (daily and roll-up)."""
    ensure_dirs()
    fd, tmp = tempfile.mkstemp(dir=_paths()["reports"], prefix=".rep-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def summarize(rows: list[dict]) -> str:
    hires = [r for r in rows if r["change_type"] == "new_hire"]
    deps = [r for r in rows if r["change_type"] == "departed"]
    parts = [f"{len(hires)} new hire(s), {len(deps)} departure(s)"]
    for label, group in (("departures", deps), ("hires", hires)):
        counts = collections.Counter(r["department"] for r in group if r["department"])
        if counts:
            top = ", ".join(f"{dept} {n}" for dept, n in counts.most_common(3))
            parts.append(f"top {label} by dept: {top}")
    return " | ".join(parts)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

class MailConfig:
    """SMTP settings from the environment.

    The app password is read once and never logged, echoed, or placed in a
    header. Most providers require an app-specific password for SMTP -- the account
    password will not authenticate once 2FA is on.
    """

    def __init__(self) -> None:
        self.host = os.environ.get("BHR_SMTP_HOST", DEFAULT_SMTP_HOST).strip()
        self.port = int(os.environ.get("BHR_SMTP_PORT", DEFAULT_SMTP_PORT))
        self.user = os.environ.get("BHR_SMTP_USER", "").strip()
        self.password = os.environ.get("BHR_SMTP_PASSWORD", "")
        self.mail_to = [a.strip() for a in
                        os.environ.get("BHR_MAIL_TO", "").split(",") if a.strip()]
        self.mail_from = os.environ.get("BHR_MAIL_FROM", "").strip() or self.user
        # PII stays out of the mailbox unless asked for: the body names only the
        # people who changed, and the change-list CSV attaches only on request.
        self.attach = os.environ.get("BHR_MAIL_ATTACH_CSV") == "1"

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.mail_to)

    def describe(self) -> str:
        return (f"{self.user or '(no user)'} -> {', '.join(self.mail_to) or '(no recipient)'} "
                f"via {self.host}:{self.port}"
                f"{' with CSV attached' if self.attach else ''}")


def _render_bodies(day: str, rows: list[dict], note: str = "", *,
                   title: str | None = None,
                   headcount: tuple[str, int, str, int] | None = None,
                   trend: list[tuple[str, int]] | None = None) -> tuple[str, str]:
    """Text and HTML bodies shared by the email, the HTML report and the
    notification click target. The keyword extras are used by roll-ups only:
    `headcount` is (start_day, start_n, end_day, end_n); `trend` is one
    (day, headcount) per snapshot in the period. Omitting them gives the
    daily output."""
    hires = [r for r in rows if r["change_type"] == "new_hire"]
    deps = [r for r in rows if r["change_type"] == "departed"]

    def by_dept(group):
        c = collections.Counter(r.get("department") or "(unknown)" for r in group)
        return c.most_common()

    text = [title or f"BambooHR org changes for {day}", "",
            f"{len(deps)} departure(s), {len(hires)} new hire(s)", ""]
    if headcount:
        d0, n0, d1, n1 = headcount
        text += [f"Headcount: {n0} -> {n1} ({n1 - n0:+d})",
                 f"Compared snapshots {d0} -> {d1}", ""]
    if note:
        text += [note, ""]
    for label, group in (("Departures", deps), ("New hires", hires)):
        if not group:
            text += [f"{label}: none", ""]
            continue
        text.append(f"{label} by department:")
        for dept, n in by_dept(group):
            text.append(f"  {n:3d}  {dept}")
        text.append("")
        text.append(f"{label}:")
        for r in sorted(group, key=lambda r: (r.get("department") or "", r["name"])):
            jt = r.get("job_title") or "-"
            mgr = r.get("supervisor_name") or "-"
            text.append(f"  {r['name']} — {jt} — {r.get('department') or '-'} (mgr: {mgr})")
        text.append("")
    if trend:
        text.append("Headcount by snapshot:")
        for d, n in trend:
            text.append(f"  {d}  {n}")
        text.append("")

    def esc(v):
        return (str(v or "")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    html = [
        "<html><body style=\"font-family:-apple-system,Segoe UI,Helvetica,sans-serif;"
        "font-size:15px;line-height:1.45;color:#111\">",
        f"<h2 style=\"margin:0 0 4px\">{esc(title) if title else 'Org changes — ' + esc(day)}</h2>",
        f"<p style=\"margin:0 0 16px;font-size:17px\"><b>{len(deps)}</b> departure(s), "
        f"<b>{len(hires)}</b> new hire(s)</p>",
    ]
    if headcount:
        d0, n0, d1, n1 = headcount
        html.append(
            f"<p style=\"margin:0 0 16px\">Headcount <b>{n0}</b> &rarr; <b>{n1}</b> "
            f"({n1 - n0:+d}) &middot; snapshots {esc(d0)} &rarr; {esc(d1)}</p>")
    if note:
        html.append(f"<p style=\"background:#fff8e1;padding:8px;border-radius:4px\">{esc(note)}</p>")
    for label, group in (("Departures", deps), ("New hires", hires)):
        html.append(f"<h3 style=\"margin:18px 0 6px\">{label} ({len(group)})</h3>")
        if not group:
            html.append("<p style=\"color:#666;margin:0\">none</p>")
            continue
        html.append("<table cellpadding=6 cellspacing=0 style=\"border-collapse:collapse;"
                    "width:100%;max-width:640px\">")
        html.append("<tr style=\"text-align:left;background:#f4f4f5\">"
                    "<th>Name</th><th>Department</th><th>Title</th><th>Manager</th></tr>")
        for r in sorted(group, key=lambda r: (r.get("department") or "", r["name"])):
            html.append(
                "<tr style=\"border-top:1px solid #e4e4e7\">"
                f"<td><b>{esc(r['name'])}</b></td><td>{esc(r.get('department'))}</td>"
                f"<td>{esc(r.get('job_title'))}</td><td>{esc(r.get('supervisor_name'))}</td></tr>")
        html.append("</table>")
    if trend:
        html.append("<h3 style=\"margin:18px 0 6px\">Headcount by snapshot</h3>")
        html.append("<table cellpadding=6 cellspacing=0 style=\"border-collapse:collapse\">")
        html.append("<tr style=\"text-align:left;background:#f4f4f5\">"
                    "<th>Snapshot</th><th>Headcount</th></tr>")
        for d, n in trend:
            html.append(f"<tr style=\"border-top:1px solid #e4e4e7\">"
                        f"<td>{esc(d)}</td><td style=\"text-align:right\">{n}</td></tr>")
        html.append("</table>")
    html.append("</body></html>")
    return "\n".join(text), "\n".join(html)


def build_message(cfg: MailConfig, subject: str, text: str, html: str | None = None,
                  attach_path: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if attach_path and os.path.exists(attach_path):
        with open(attach_path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype="text", subtype="csv",
                               filename=os.path.basename(attach_path))
    return msg


def send_message(cfg: MailConfig, msg: EmailMessage) -> None:
    if not cfg.configured:
        raise TrackerError(
            "email not configured: set BHR_SMTP_USER, BHR_SMTP_PASSWORD "
            "(an app-specific password, not your account password) and BHR_MAIL_TO."
        )
    ctx = ssl.create_default_context()
    try:
        if cfg.port == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=60) as smtp:
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=60) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ctx)
                smtp.ehlo()
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        # never echo the password; the server's own text is safe to show
        raise TrackerError(
            f"SMTP login rejected by {cfg.host} for {cfg.user}: {exc.smtp_code}. "
            "Most providers need an app-specific password with SMTP access."
        ) from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise TrackerError(f"SMTP send failed via {cfg.host}:{cfg.port}: {exc}") from exc


def email_report(day: str, rows: list[dict], report_file: str | None,
                 note: str = "") -> None:
    cfg = MailConfig()
    if not cfg.configured:
        log("email not configured; skipping (set BHR_SMTP_* to enable)")
        return
    hires = sum(1 for r in rows if r["change_type"] == "new_hire")
    deps = len(rows) - hires
    subject = (f"BambooHR: {deps} departure(s), {hires} new hire(s) — {day}"
               if rows else f"BambooHR: no org changes — {day}")
    text, html = _render_bodies(day, rows, note)
    msg = build_message(cfg, subject, text, html,
                        attach_path=report_file if cfg.attach else None)
    send_message(cfg, msg)
    log(f"emailed report: {cfg.describe()}")


def email_failure(day: str, error: str, what: str = "run") -> None:
    """A dead run must be louder than a quiet one -- absence of mail is the
    only signal left if this path stays silent. `what` is "run" for the daily
    pull or "roll-up" for a weekly/monthly roll-up."""
    cfg = MailConfig()
    if not cfg.configured:
        return
    if what == "roll-up":
        text = (f"The BambooHR org tracker's roll-up FAILED on {day}.\n\n{error}\n\n"
                "No roll-up report was produced. `--rollup auto` retries it on the "
                "next scheduled run; the daily reports are unaffected.\n")
        subject = f"BambooHR tracker roll-up FAILED — {day}"
    else:
        text = (f"The BambooHR org tracker FAILED on {day}.\n\n{error}\n\n"
                f"{snapshot_state(day)[0]}\n")
        subject = f"BambooHR tracker FAILED — {day}"
    try:
        send_message(cfg, build_message(cfg, subject, text))
        log("emailed failure notice")
    except TrackerError as exc:
        log(f"could not email failure notice: {exc}")


# The snapshot path this process's --run wrote, if any: lets a failure message
# say whether the snapshot on disk for the day is this run's or an earlier one's.
_run_wrote: str | None = None


def snapshot_state(day: str) -> tuple[str, str]:
    """(email text, notification text) saying what a failed daily run left on
    disk for `day`. Stated from the facts, never assumed: a run can fail after
    it saved its snapshot (a report or mail step), or a same-day rerun can fail
    while an earlier run's snapshot stands."""
    snap = snapshot_path(day)
    name = os.path.basename(snap)
    if _run_wrote == snap:
        return (f"This run saved the roster ({name}) before it failed, so the next "
                "run compares against it and will NOT report today's changes again. "
                "To get today's report, run `python3 org_tracker.py --diff-only` "
                "today; the weekly roll-up still counts these changes.",
                "Roster saved, a later step failed -- run --diff-only today.")
    if os.path.exists(snap):
        try:
            count = f"{len(read_snapshot(snap))} people"
        except (OSError, ValueError, csv.Error):
            count = "unreadable"
        return (f"This run's roster was NOT saved. An earlier run's snapshot for {day} "
                f"({name}, {count}) stands, and the next run compares against it.",
                "This run's roster was not saved; an earlier one stands.")
    return (f"No snapshot for {day} is on disk, so no changes were missed -- but "
            "nothing was checked either. The next scheduled run will try again.",
            "Nothing was checked.")


# --------------------------------------------------------------------------
# macOS notification
# --------------------------------------------------------------------------

# A banner can be read by anyone glancing at the screen, and this data is a
# personnel record. Counts and department names go in the banner; individual
# names stay in the report the click opens.
NOTIFY_TITLE = "Org changes"


def is_macos() -> bool:
    return sys.platform == "darwin"


def notifier_app() -> str:
    """terminal-notifier's app bundle under whichever Homebrew prefix installed
    it: /opt/homebrew on Apple silicon, /usr/local on Intel."""
    tn = shutil.which("terminal-notifier")
    prefix = os.path.dirname(os.path.dirname(tn)) if tn else "/opt/homebrew"
    return os.path.join(prefix, "opt", "terminal-notifier", "terminal-notifier.app")


def write_html_report(rows: list[dict], day: str, note: str = "", *,
                      path: str | None = None, **render_kwargs) -> str:
    """A readable report for the notification click to open.

    Reuses the email renderer, so the notification and the email always show
    the same thing -- one place to change the presentation. Roll-ups pass
    their own `path` and renderer extras.
    """
    ensure_dirs()
    path = path or os.path.join(_paths()["reports"], f"changes-{day}.html")
    _, html = _render_bodies(day, rows, note, **render_kwargs)
    fd, tmp = tempfile.mkstemp(dir=_paths()["reports"], prefix=".rep-", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def _notify_summary(rows: list[dict]) -> tuple[str, str]:
    hires = [r for r in rows if r["change_type"] == "new_hire"]
    deps = [r for r in rows if r["change_type"] == "departed"]
    if not rows:
        return "No changes today", "Roster checked, nothing moved"
    subtitle = f"{len(deps)} departure(s), {len(hires)} new hire(s)"
    counts = collections.Counter(
        r.get("department") or "(unknown)" for r in deps) or collections.Counter(
        r.get("department") or "(unknown)" for r in hires)
    top = ", ".join(f"{dept} {n}" for dept, n in counts.most_common(3))
    return subtitle, (f"Most affected: {top}" if top else "Click to open the report")


def notify_macos(day: str, rows: list[dict], open_path: str | None,
                 subtitle: str | None = None, message: str | None = None) -> str:
    """Post a notification. Returns "clickable" (terminal-notifier), "plain"
    (osascript), or "" when nothing was posted.

    `terminal-notifier` is used when present because it is the only one of the
    two that makes the banner CLICKABLE (-open). Plain `osascript` notifications
    cannot carry a click action -- clicking one does nothing at all -- so the
    fallback posts the summary and says where the report is instead.

    Every argument is passed as a list element, never interpolated into a
    shell string: department names come from BambooHR and are untrusted input.
    """
    if not is_macos():
        log("not macOS; skipping notification")
        return ""
    if subtitle is None or message is None:
        subtitle, message = _notify_summary(rows)

    tn = shutil.which("terminal-notifier")
    reason = "terminal-notifier not installed (brew install terminal-notifier)"
    if tn:
        args = [tn, "-title", NOTIFY_TITLE, "-subtitle", subtitle,
                "-message", message, "-group", "bhr-org-tracker"]
        if open_path:
            args += ["-open", "file://" + urllib.parse.quote(os.path.abspath(open_path))]
        proc = subprocess.run(args, capture_output=True, check=False)
        if proc.returncode == 0:
            log(f"notified (clickable): {subtitle}")
            return "clickable"
        err = proc.stderr.decode()[:200]
        # The common one: macOS has no permission record because the app bundle
        # has never been launched, so it cannot even ask. One `open` fixes it
        # for good -- see register_notifier().
        if "not allowed" in err or "permission" in err.lower():
            reason = ("terminal-notifier lacks notification permission -- run "
                      f"`open {notifier_app()}` once, then allow it in System "
                      "Settings > Notifications")
        else:
            reason = f"terminal-notifier failed rc={proc.returncode}: {err}"
        log(reason)

    # Fallback: osascript notifications cannot carry a click action at all, so
    # the report path goes into the text instead.
    where = f" -- report: {os.path.basename(open_path)}" if open_path else ""
    script = (f'display notification {_as_applescript(message + where)} '
              f'with title {_as_applescript(NOTIFY_TITLE)} '
              f'subtitle {_as_applescript(subtitle)}')
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, check=False)
    if proc.returncode != 0:
        log(f"notification failed entirely: {proc.stderr.decode()[:200]}")
        return ""
    log(f"notified (NOT clickable): {subtitle}")
    return "plain"


def register_notifier() -> bool:
    """Launch terminal-notifier's app bundle once so macOS creates a
    notification-permission record for it.

    Without this, a freshly brewed terminal-notifier fails with "Notifications
    are not allowed for this application" and never appears in System Settings
    to be allowed -- it cannot request permission it was never registered to
    ask for. One `open` is the whole fix.
    """
    app = notifier_app()
    if not is_macos() or not os.path.isdir(app):
        return False
    proc = subprocess.run(["open", app], capture_output=True, check=False)
    return proc.returncode == 0


def _as_applescript(value: str) -> str:
    """Quote a string for AppleScript. Backslash first, then quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


# --------------------------------------------------------------------------
# Roll-ups (weekly / monthly)
# --------------------------------------------------------------------------
#
# A roll-up is the daily presence/absence diff taken between two snapshots
# further apart. Period boundaries are fixed calendar anchors, and every
# boundary resolves to "the latest snapshot on or before it" -- so the end of
# one period is exactly the start of the next, and a day the laptop was closed
# can neither drop a change nor count it twice.
#
# The schedule lives here rather than in launchd/systemd: `--rollup auto`
# decides what is due from today's date and from which report files already
# exist, which makes "first weekday of the month" a tested function, gives
# catch-up for free (a missed Monday is produced on Tuesday), and makes a
# second run the same day a no-op.

def first_weekday(year: int, month: int) -> dt.date:
    """First Monday-to-Friday of the month. Public holidays are not
    considered: this is a report, and a day early on a holiday is fine."""
    d = dt.date(year, month, 1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def _month_end(year: int, month: int) -> dt.date:
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def weekly_period(today: dt.date) -> dict:
    """This week's Monday versus the Monday before it."""
    anchor = today - dt.timedelta(days=today.weekday())
    return {
        "kind": "weekly", "noun": "week", "anchor": anchor.isoformat(),
        "start_bound": (anchor - dt.timedelta(days=7)).isoformat(),
        "end_bound": anchor.isoformat(),
        "label": f"week ending {anchor.isoformat()}",
    }


def monthly_period(today: dt.date) -> dict:
    """The previous calendar month: its last snapshot versus the last snapshot
    of the month before it."""
    y, m = _prev_month(today.year, today.month)
    py, pm = _prev_month(y, m)
    return {
        "kind": "monthly", "noun": "month", "anchor": f"{y:04d}-{m:02d}",
        "start_bound": _month_end(py, pm).isoformat(),
        "end_bound": _month_end(y, m).isoformat(),
        "label": dt.date(y, m, 1).strftime("%B %Y"),
    }


def rollup_paths(period: dict) -> tuple[str, str]:
    base = os.path.join(_paths()["reports"], f"rollup-{period['kind']}-{period['anchor']}")
    return base + ".csv", base + ".html"


def list_snapshots() -> list[tuple[str, str]]:
    """(day, path) for every snapshot, oldest first."""
    ensure_dirs()
    found = []
    for name in os.listdir(_paths()["snapshots"]):
        m = re.fullmatch(r"snapshot-(\d{4}-\d{2}-\d{2})\.csv", name)
        if m:
            found.append((m.group(1), os.path.join(_paths()["snapshots"], name)))
    return sorted(found)


def snapshot_on_or_before(day: str, snaps: list[tuple[str, str]] | None = None
                          ) -> tuple[str, str] | None:
    cands = [s for s in (list_snapshots() if snaps is None else snaps) if s[0] <= day]
    return cands[-1] if cands else None


def rollups_due(today: dt.date) -> list[dict]:
    """What `--rollup auto` should produce today: each period whose anchor has
    passed, that has at least one snapshot on or before its end (otherwise the
    tracker did not exist yet and there is nothing to say), and whose report
    file does not exist yet."""
    snaps = list_snapshots()
    candidates = [weekly_period(today)]
    if today >= first_weekday(today.year, today.month):
        candidates.append(monthly_period(today))
    due = []
    for p in candidates:
        if os.path.exists(rollup_paths(p)[0]):
            continue
        if not snapshot_on_or_before(p["end_bound"], snaps):
            continue
        due.append(p)
    return due


def resolve_period(period: dict) -> dict:
    """Attach the actual snapshot pair to a period. Raises when no snapshot
    exists on or before the period's end; falls back to the earliest snapshot,
    with a note, when history is younger than the period."""
    snaps = list_snapshots()
    end = snapshot_on_or_before(period["end_bound"], snaps)
    if not end:
        raise TrackerError(
            f"{period['kind']} roll-up for {period['label']}: no snapshot on or "
            f"before {period['end_bound']}")
    start = snapshot_on_or_before(period["start_bound"], snaps)
    note = ""
    if not start:
        start = snaps[0]
        note = (f"History begins {start[0]}, so this covers less than a full "
                f"{period['noun']}.")
    if start[0] == end[0]:
        note = (f"Only one snapshot ({end[0]}) falls in this {period['noun']}; "
                "there is nothing to compare against yet.")
    span = [s for s in snaps if start[0] <= s[0] <= end[0]]
    return dict(period, start=start, end=end, note=note, span=span)


def run_rollup(period: dict, email: bool = False, notify: bool = False) -> str:
    """Produce one roll-up: CSV + HTML on disk, optional email/notification.
    Returns the CSV path."""
    res = resolve_period(period)
    (d0, p0), (d1, p1) = res["start"], res["end"]
    old, new = read_snapshot(p0), read_snapshot(p1)
    trend = [(d, len(read_snapshot(p))) for d, p in res["span"]]
    rows = [] if d0 == d1 else diff_snapshots(
        old, new, d1, allow_shrink=shrink_allowed()
        or (shrank(len(old), len(new)) and accepted_drops_allow(trend)))
    headcount = (d0, len(old), d1, len(new))
    title = f"BambooHR {period['kind']} org changes — {period['label']}"
    csv_path, html_path = rollup_paths(period)
    write_rows_csv(rows, csv_path)
    write_html_report(rows, d1, res["note"], path=html_path,
                      title=title, headcount=headcount, trend=trend)
    log(f"{period['kind']} roll-up {period['label']}: {summarize(rows)} | "
        f"headcount {len(old)} -> {len(new)} ({d0} -> {d1}) -> {csv_path}")
    if email:
        email_rollup(period, rows, headcount, trend, res["note"], csv_path)
    if notify:
        hires = sum(1 for r in rows if r["change_type"] == "new_hire")
        deps = len(rows) - hires
        notify_macos(d1, rows, html_path,
                     subtitle=f"{period['kind'].capitalize()} roll-up: "
                              f"{deps} departure(s), {hires} new hire(s)",
                     message=f"{period['label']} · headcount {len(old)} → {len(new)} "
                             f"({len(new) - len(old):+d})")
    return csv_path


def email_rollup(period: dict, rows: list[dict], headcount: tuple[str, int, str, int],
                 trend: list[tuple[str, int]], note: str, csv_path: str) -> None:
    cfg = MailConfig()
    if not cfg.configured:
        log("email not configured; skipping roll-up email (set BHR_SMTP_* to enable)")
        return
    hires = sum(1 for r in rows if r["change_type"] == "new_hire")
    deps = len(rows) - hires
    d0, n0, d1, n1 = headcount
    if d0 == d1:
        subject = f"BambooHR {period['kind']}: insufficient history — {period['label']}"
    else:
        subject = (f"BambooHR {period['kind']}: {deps} departure(s), {hires} new hire(s), "
                   f"headcount {n0}→{n1} ({n1 - n0:+d}) — {period['label']}")
    title = f"BambooHR {period['kind']} org changes — {period['label']}"
    text, html = _render_bodies(d1, rows, note, title=title,
                                headcount=headcount, trend=trend)
    msg = build_message(cfg, subject, text, html,
                        attach_path=csv_path if cfg.attach else None)
    send_message(cfg, msg)
    log(f"emailed {period['kind']} roll-up: {cfg.describe()}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def wait_for_network(host: str | None, port: int = 443,
                     budget: int = NET_WAIT_SECONDS) -> bool:
    """Block until a TCP connection to `host` succeeds, or the budget expires.

    The scheduled run starts when the machine wakes, not at the scheduled
    minute, so it frequently begins before the network is usable. Without this
    the first request connects against a half-ready stack and then stalls in
    read() for the full HTTP timeout. Returns True if the host was reachable;
    a False return is not fatal -- the request still runs and produces a real
    error message rather than this guess.
    """
    if not host:
        return False
    start = time.monotonic()
    deadline = start + budget
    announced = False
    while True:
        try:
            with socket.create_connection((host, port), timeout=5):
                if announced:
                    log(f"network ready after {time.monotonic() - start:.0f}s")
                return True
        except OSError as exc:
            if time.monotonic() >= deadline:
                log(f"network still unreachable after {budget}s ({exc}); "
                    "attempting the run anyway")
                return False
            if not announced:
                log(f"waiting for the network to reach {host} ...")
                announced = True
            time.sleep(3)


def acquire_roster(sess: Session, root_id: str | None) -> tuple[list[Person], str]:
    """Try the durable path first, then the session path.

    When the API fails and the web fallback is not set up, the API's own
    reason is what gets raised, so a revoked key is reported as a revoked key
    and not as a missing `--root`.
    """
    api_error = None
    if sess.api_key:
        try:
            people = fetch_roster_api(sess)
            log(f"roster via API directory: {len(people)} people")
            return people, "api"
        except TrackerError as exc:
            if not (sess.cookie and root_id):
                reason = str(exc).rstrip(".")
                raise TrackerError(
                    f"API roster unavailable: {reason}. Next step: fix that, then confirm "
                    "with `org_tracker.py --check-auth` (no web fallback is configured)."
                ) from exc
            log(f"API roster unavailable ({exc}); falling back to web export")
            api_error = exc
    elif not (sess.cookie and root_id):
        raise TrackerError(
            "No roster source is configured: BHR_PERSONAL_API_KEY is not set, and "
            "the web fallback needs both BHR_SESSION_COOKIE and --root. Next step: "
            "set an API key, or set the cookie and pass the top-of-chart employee id."
        )
    try:
        people = fetch_roster_web(sess, root_id)
    except TrackerError as exc:
        if api_error is not None:
            raise TrackerError(
                f"API roster unavailable: {api_error}; web fallback also failed: {exc}"
            ) from exc
        raise
    log(f"roster via web CSV export from root {root_id}: {len(people)} people")
    return people, "web"


def harvest_attributes(sess: Session, people: list[Person], forest: Forest,
                       plan: list[str], day: str) -> None:
    """Fetch each planned export and merge title/department/division."""
    missing = [p for p in people if not p.get("Department") or not p.get("Job Title")]
    if not missing:
        log("roster already carries department and title -- no chart exports needed")
        return
    log(f"{len(missing)} people missing attributes; fetching {len(plan)} export(s)")
    ensure_dirs()
    total_filled = 0
    for n, root in enumerate(plan, 1):
        scope = forest.descendants(root, MAX_LEVELS)
        blob = sess.get_web(
            f"/employees/exportable_org_chart/pdf/{root}",
            chart_params(MAX_LEVELS) + [("type", "pdf")],
        )
        path = os.path.join(_paths()["exports"], f"{day}-root{root}.pdf")
        with open(path, "wb") as fh:
            fh.write(blob)
        os.chmod(path, 0o600)
        cards, repairs = parse_org_pdf(path, expected=len(scope))
        filled = merge_attributes(people, cards, scope_ids=scope)
        total_filled += filled
        log(f"  [{n}/{len(plan)}] root {root}: {len(cards)} cards, {filled} fields filled"
            + (f", {len(repairs)} repair(s)" if repairs else ""))
        for r in repairs[:5]:
            log(f"      repair: {r}")
    log(f"attribute harvest filled {total_filled} field(s)")


def cmd_run(args) -> int:
    global _run_wrote
    _run_wrote = None
    day = args.day or dt.date.today().isoformat()
    sess = session_from_env()
    # Before anything that needs the network -- including the failure mail this
    # run might have to send.
    wait_for_network(urllib.parse.urlsplit(
        sess.api_host if sess.api_key else sess.host).hostname)
    people, source = acquire_roster(sess, args.root)
    need = [p for p in people if not p.get("Department") or not p.get("Job Title")]
    gap = len(need) / len(people)
    if gap > ATTR_GAP_THRESHOLD:
        forest = Forest(people)
        plan = plan_cover(forest, args.levels)
        log(f"cover plan: {len(plan)} export(s) for {len(people)} people "
            f"across {len(forest.roots)} component(s)")
        # Attribute enrichment is optional: a roster failure must abort the run
        # (a silently empty roster reads as a mass departure), but missing
        # departments for some people is a degraded report, not a wrong one.
        try:
            harvest_attributes(sess, people, forest, plan, day)
        except TrackerError as exc:
            log(f"WARNING: attribute harvest unavailable ({exc}); "
                f"continuing with {len(need)} incomplete record(s)")
    elif need:
        log(f"{len(need)} of {len(people)} people lack department or title "
            f"({gap:.1%}) -- below the {ATTR_GAP_THRESHOLD:.0%} crawl threshold, "
            "skipping chart exports")
    else:
        log(f"roster carries department and title for all {len(people)} people "
            "-- no chart exports needed")

    prior = prior_snapshot(day)
    old = read_snapshot(prior) if prior else []
    # Diff BEFORE writing: a roster the shrink guard refuses must leave no
    # snapshot behind (and must not replace one already taken today), or the
    # next run reports everyone missing from it as a new hire.
    rows = diff_snapshots(old, snapshot_rows(people), day,
                          allow_shrink=shrink_allowed()) if prior else []
    accepted = shrank(len(old), len(people))   # only reachable with the override set
    if accepted:   # before the snapshot, so a marker failure leaves nothing half-done
        record_accepted_drop(day, len(old), len(people))
    path = _run_wrote = write_snapshot(people, day)
    log(f"snapshot written: {path} ({len(people)} people)")
    if accepted:
        log(f"roster drop {len(old)} -> {len(people)} accepted (BHR_ALLOW_SHRINK=1); "
            "roll-ups spanning it will accept it too")
    else:
        # Retract a same-day acceptance this rerun no longer needs. The marker
        # only matters to roll-ups, so trouble with it never blocks the report.
        try:
            clear_accepted_drop(day)
        except TrackerError as exc:
            log(f"WARNING: {exc} -- continuing; only roll-ups read it")
    incomplete = (f"{len(need)} record(s) lack a department or job title in this run."
                  if need else "")
    if not prior:
        log("baseline snapshot saved; no prior snapshot to compare -- "
            "reporting starts with the next run")
        baseline_note = (f"Baseline snapshot saved ({len(people)} people). "
                         "Change reporting starts with the next run.")
        if args.email:
            email_report(day, [], None, note=baseline_note)
        if args.notify:
            notify_macos(day, [], write_html_report([], day, baseline_note),
                         subtitle="Baseline saved",
                         message=f"{len(people)} people; reporting starts tomorrow")
        return 0
    rpath = write_report(rows, day)
    log(f"{summarize(rows)} vs {os.path.basename(prior)} -> {rpath}")
    if args.email:
        email_report(day, rows, rpath, note=incomplete)
    if args.notify:
        notify_macos(day, rows, write_html_report(rows, day, incomplete))
    return 0


def cmd_plan(args) -> int:
    if args.plan_from:
        with open(args.plan_from, "rb") as fh:
            people = roster_from_csv_bytes(fh.read())
        source = args.plan_from
    else:
        sess = session_from_env()
        people, source = acquire_roster(sess, args.root)
    forest = Forest(people)
    plan = plan_cover(forest, args.levels)
    sizes = [(r, len(forest.descendants(r, args.levels))) for r in plan]
    sizes.sort(key=lambda t: -t[1])
    covered = set()
    for r in plan:
        covered |= forest.descendants(r, args.levels)
    print(f"roster source: {source}")
    print(f"people: {len(people)} | components: {len(forest.roots)} | "
          f"max levels per export: {args.levels}")
    print(f"exports needed: {len(plan)} | coverage: {len(covered)}/{len(people)}")
    multi = [t for t in sizes if t[1] > 1]
    print(f"  multi-person exports: {len(multi)} | single-person exports: {len(sizes) - len(multi)}")
    for r, n in sizes[:15]:
        print(f"   root {r:>8}  {forest.by_id[r]['Name'][:28]:28} covers {n}")
    return 0


def cmd_check_auth(args) -> int:
    sess = session_from_env()
    print(f"subdomain: {sess.subdomain or '(MISSING -- set BHR_SUBDOMAIN)'}")
    print(f"cookie:    {'present' if sess.cookie else 'absent'}")
    print(f"api key:   {'present' if sess.api_key else 'absent'}")
    print(f"pdftotext: {'found' if pdftotext_available() else 'MISSING (brew install poppler)'}")
    ok = True
    if sess.api_key:
        try:
            people = fetch_roster_api(sess)
            depts = sum(1 for p in people if p["Department"])
            print(f"API directory: OK -- {len(people)} people, {depts} with a department")
            if depts > len(people) * 0.9:
                print("  => API path is sufficient on its own; no chart exports needed.")
        except TrackerError as exc:
            ok = False
            print(f"API directory: FAIL -- {exc}")
    if sess.cookie and args.root:
        try:
            blob = sess.get_web(
                f"/employees/exportable_org_chart/csv/{args.root}",
                chart_params(args.levels) + [("type", "csv")],
            )
            rows = roster_from_csv_bytes(blob)
            depts = sum(1 for r in rows if r["Department"])
            print(f"web CSV export: OK -- {len(rows)} rows, {depts} with a department")
        except TrackerError as exc:
            ok = False
            print(f"web CSV export: FAIL -- {exc}")
    elif sess.cookie:
        print("web CSV export: skipped (pass --root <employeeId> to probe it)")
    return 0 if ok else 1


def cmd_diff_only(args) -> int:
    ensure_dirs()
    snaps = sorted(
        os.path.join(_paths()["snapshots"], n)
        for n in os.listdir(_paths()["snapshots"])
        if re.fullmatch(r"snapshot-\d{4}-\d{2}-\d{2}\.csv", n)
    )
    if len(snaps) < 2:
        raise TrackerError("need at least two snapshots to diff.")
    old, new = snaps[-2], snaps[-1]
    d0, day = (re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(p)).group(1)
               for p in (old, new))
    old_rows, new_rows = read_snapshot(old), read_snapshot(new)
    rows = diff_snapshots(old_rows, new_rows, day,
                          allow_shrink=shrink_allowed()
                          or (shrank(len(old_rows), len(new_rows)) and accepted_drops_allow(
                              [(d0, len(old_rows)), (day, len(new_rows))])))
    print(f"{summarize(rows)} ({os.path.basename(old)} -> {os.path.basename(new)})")
    print(write_report(rows, day))
    return 0


def cmd_parse_pdf(args) -> int:
    cards, repairs = parse_org_pdf(args.parse_pdf)
    print(f"{len(cards)} card(s) parsed from {os.path.basename(args.parse_pdf)}")
    print(f"divisions: {dict(collections.Counter(c['div'] for c in cards))}")
    for r in repairs:
        print(f"repair: {r}")
    if args.verbose:
        for c in cards:
            print(f"  {c['name']} | {c['title']} | {c['dept']} | {c['div']}")
    return 0


def cmd_test_notify(args) -> int:
    day = args.day or dt.date.today().isoformat()
    sample = [
        {"change_type": "departed", "person_id": "1", "name": "Sample Departure",
         "job_title": "Staff Engineer", "department": "Platform Engineering",
         "division": "Tech", "supervisor_name": "A Manager", "detected_date": day},
        {"change_type": "departed", "person_id": "3", "name": "Second Departure",
         "job_title": "Analyst", "department": "Platform Engineering",
         "division": "Tech", "supervisor_name": "A Manager", "detected_date": day},
        {"change_type": "new_hire", "person_id": "2", "name": "Sample Hire",
         "job_title": "Security Engineer", "department": "Security",
         "division": "Tech", "supervisor_name": "A Manager", "detected_date": day},
    ]
    # Its own file: the sample must never replace the real changes-<day>.html
    # that today's notification opens.
    path = write_html_report(sample, day, note="This is a test notification.",
                             path=os.path.join(_paths()["reports"], "changes-test.html"))
    print(f"sample report: {path}")
    if shutil.which("terminal-notifier") and register_notifier():
        print(f"registered {os.path.basename(notifier_app())} with macOS")
    how = notify_macos(day, sample, path)
    if not how:
        raise TrackerError("no notification was posted")
    print("posted -- click it to open the sample report" if how == "clickable" else
          f"posted, but it is NOT clickable (see tracker.log for why); open {path} to see it")
    return 0


def cmd_rollup(args) -> int:
    today = dt.date.fromisoformat(args.day) if args.day else dt.date.today()
    if args.rollup == "auto":
        periods = rollups_due(today)
        if not periods:
            log(f"no roll-up due on {today.isoformat()}")
            return 0
    elif args.rollup == "weekly":
        periods = [weekly_period(today)]
    else:
        periods = [monthly_period(today)]
    for period in periods:
        run_rollup(period, email=args.email, notify=args.notify)
    return 0


def cmd_test_email(args) -> int:
    cfg = MailConfig()
    print(f"smtp: {cfg.describe()}")
    if not cfg.configured:
        raise TrackerError(
            "email not configured: set BHR_SMTP_USER, BHR_SMTP_PASSWORD "
            "(an app-specific password) and BHR_MAIL_TO."
        )
    day = args.day or dt.date.today().isoformat()
    sample = [
        {"change_type": "departed", "person_id": "1", "name": "Sample Departure",
         "job_title": "Staff Engineer", "department": "Platform Engineering",
         "division": "Tech", "supervisor_name": "A Manager", "detected_date": day},
        {"change_type": "new_hire", "person_id": "2", "name": "Sample Hire",
         "job_title": "Security Engineer", "department": "Security",
         "division": "Tech", "supervisor_name": "A Manager",
         "detected_date": day},
    ]
    text, html = _render_bodies(day, sample, note="This is a test message.")
    send_message(cfg, build_message(cfg, f"BambooHR tracker test — {day}", text, html))
    print("sent")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="full pull, snapshot, diff, report")
    ap.add_argument("--plan", action="store_true", help="print the export plan, fetch nothing")
    ap.add_argument("--plan-from", metavar="CSV", help="plan against a roster CSV on disk")
    ap.add_argument("--check-auth", action="store_true", help="probe credentials, mutate nothing")
    ap.add_argument("--diff-only", action="store_true", help="re-diff the two newest snapshots")
    ap.add_argument("--parse-pdf", metavar="PDF", help="parse one export and print records")
    ap.add_argument("--root", help="employee id at the top of the chart")
    ap.add_argument("--levels", type=int, default=MAX_LEVELS,
                    help=f"levels per export (default {MAX_LEVELS}, the endpoint ceiling)")
    ap.add_argument("--rollup", choices=("weekly", "monthly", "auto"),
                    help="produce a roll-up over stored snapshots: weekly (this week's "
                         "Monday vs the one before), monthly (previous calendar month), "
                         "or auto (whatever is due today and not yet produced -- the "
                         "monthly is due from the first weekday of the month)")
    ap.add_argument("--day", help="override today's date (YYYY-MM-DD)")
    ap.add_argument("--email", action="store_true",
                    help="email the report (needs BHR_SMTP_*); failures email too")
    ap.add_argument("--test-email", action="store_true",
                    help="send one sample email and exit, mutating nothing")
    ap.add_argument("--notify", action="store_true",
                    help="post a macOS notification; clicking it opens the report")
    ap.add_argument("--test-notify", action="store_true",
                    help="post one sample notification and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.test_email:
            return cmd_test_email(args)
        if args.test_notify:
            return cmd_test_notify(args)
        if args.parse_pdf:
            return cmd_parse_pdf(args)
        if args.plan or args.plan_from:
            return cmd_plan(args)
        if args.check_auth:
            return cmd_check_auth(args)
        if args.diff_only:
            return cmd_diff_only(args)
        if args.rollup:
            return cmd_rollup(args)
        if args.run:
            return cmd_run(args)
        ap.print_help()
        return 2
    except TrackerError as exc:
        log(f"ERROR: {exc}")
        day = args.day or dt.date.today().isoformat()
        if args.email and (args.run or args.rollup):
            email_failure(day, str(exc), what="roll-up" if args.rollup else "run")
        if args.notify and (args.run or args.rollup):
            notify_macos(day, [], _paths()["log"],
                         subtitle="Roll-up FAILED" if args.rollup else "Tracker FAILED",
                         message=("Nothing was checked." if args.rollup else snapshot_state(day)[1])
                         + " Click for the log.")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
