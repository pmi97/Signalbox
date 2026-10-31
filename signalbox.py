#!/usr/bin/env python3
"""Signalbox: the interlocking board for teams of humans and AI agents.

A fork of Kanvas (https://github.com/XMihura/Kanvas) that moves the board onto
GitHub Issues so many people and many agents share one plan, and that guards
every piece of work with an interlock: an atomic lock held in git itself, a
dedicated git worktree, a lease that expires when its holder disappears, and a
state machine the CLI enforces. Wayfinder-style maps (destination, fog of war,
frontier, decision tickets) sit on the same board.

    python signalbox.py init          # set up a project
    python signalbox.py status        # what is happening, who holds what
    python signalbox.py claim SB-12   # lock a task and get a worktree for it
    python signalbox.py serve         # the shared board in your browser

Python 3.8+, standard library only. `python signalbox.py -h` lists the commands.
"""

import argparse
import fnmatch
import getpass
import hmac
import http.cookies
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlparse

if __package__:  # installed: the Kanvas engine is the kanvas_board package beside this one
    import kanvas_board as K
else:  # run as a script, or as a project's agent copy: kanvas.py sits next to this file
    sys.dont_write_bytecode = True  # no __pycache__ in the project's .agents/skills/signalbox/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kanvas as K

VERSION = "0.1.0"
FORMAT = 1
INSTALLED = __name__ == "signalbox"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_FILE = os.path.join(HERE, "SKILL.md") if os.path.isfile(os.path.join(HERE, "SKILL.md")) else os.path.join(HERE, "skill", "signalbox", "SKILL.md")
GUARD_FILE = os.path.join(HERE, "signalbox-guard.yml") if os.path.isfile(os.path.join(HERE, "signalbox-guard.yml")) \
    else os.path.join(HERE, "templates", "signalbox-guard.yml")

STATE_DIR = ".signalbox"
CONFIG_FILE = "config.json"
MIRROR_FILE = "board.json"
DEFAULT_PORT = 8790

MARK = "signalbox"                      # every Signalbox issue carries this label
STATE_LABELS = {"proposed": "sb:proposed", "todo": "sb:todo", "doing": "sb:doing", "review": "sb:review"}
LABEL_STATES = {v: k for k, v in STATE_LABELS.items()}
YOLO_LABEL = "sb:yolo"
LOCK_LABEL = "sb:locked"
LANE_PREFIX = "lane:"
KIND_PREFIX = "kind:"

# Kinds of work. `build` is ordinary work; the four decision kinds are Wayfinder's ticket types, and `map` is the
# index issue of a Wayfinder map. HITL kinds resolve only in a live exchange with a person.
KINDS = ("build", "research", "prototype", "grilling", "task", "map")
DECISION_KINDS = ("research", "prototype", "grilling", "task")
HITL_KINDS = ("grilling", "prototype")

MAP_SECTIONS = ("Destination", "Notes", "Decisions so far", "Not yet specified", "Out of scope")
# The meta block is usually last, but people and bots append to issue bodies, so it is found anywhere.
META_RE = re.compile(r"[ \t]*<!-- signalbox:meta (\{.*?\}) -->[ \t]*", re.S)
NOTE_RE = re.compile(r"\A<!-- signalbox:note (\{.*?\}) -->\n?", re.S)
REF_RE = re.compile(r"^\s*(?:[A-Za-z][A-Za-z0-9]*-|#)?(\d+)\s*$")

LABEL_COLORS = {
    MARK: "5b5bd6", "sb:proposed": "a855f7", "sb:todo": "3b82f6", "sb:doing": "f97316", "sb:review": "ec4899",
    YOLO_LABEL: "eab308", LOCK_LABEL: "64748b",
}
LABEL_HELP = {
    MARK: "Managed by Signalbox", "sb:proposed": "Signalbox: proposed, waits for a human to approve it",
    "sb:todo": "Signalbox: approved", "sb:doing": "Signalbox: in progress, held by an interlock",
    "sb:review": "Signalbox: finished, waits for human review", YOLO_LABEL: "Signalbox: YOLO chain",
    LOCK_LABEL: "Signalbox: an interlock holds this task",
}

DEFAULT_CONFIG = {
    "signalbox": FORMAT,
    "title": "",
    "prefix": "SB",
    "hub": {"type": "file", "path": ".signalbox/hub.json"},
    "approvers": [],
    "teams": {},
    "lanes": [],
    "agents": {"lease_minutes": 120, "max_claims": 2, "hitl_needs_pair": True, "logins": []},
    "locks": {"remote": "auto", "namespace": "refs/signalbox/locks"},
    "worktrees": {"dir": "../{repo}.worktrees", "base": "", "copy": [], "setup": "", "port_base": 4100},
    "paths": "warn",
    "poll_seconds": 15,
}


class SignalboxError(K.KanvasError):
    pass


class Locked(SignalboxError):
    def __init__(self, message, record=None, sha=None):
        super().__init__(message)
        self.record, self.sha = record or {}, sha


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def span(seconds):
    """3h05m, 12m, 40s."""
    seconds = int(abs(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def slug(text, limit=40):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:limit].rstrip("-") or "task"


def login_of(name):
    return str(name or "").strip().lstrip("@").lower()


def is_human_name(by):
    return by == "human" or str(by or "").startswith("@")


def dumps_meta(meta):
    # ">" is escaped so a value can never close the HTML comment that carries it.
    return json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False).replace(">", "\\u003e")


def split_body(body):
    """(description, meta) of an issue body."""
    body = (body or "").replace("\r\n", "\n")
    found = list(META_RE.finditer(body))
    if not found:
        return body.strip(), {}
    m = found[-1]
    try:
        meta = json.loads(m.group(1))
    except ValueError:
        meta = {}
    desc = re.sub(r"\n{3,}", "\n\n", (body[:m.start()].rstrip() + "\n\n" + body[m.end():].lstrip()).strip())
    return desc, meta if isinstance(meta, dict) else {}


def join_body(desc, meta):
    desc = (desc or "").strip()
    meta = {k: v for k, v in meta.items() if v not in (None, [], "", False)}
    if not meta:
        return desc
    return (desc + "\n\n" if desc else "") + f"<!-- signalbox:meta {dumps_meta(meta)} -->"


def number_of(ref):
    m = REF_RE.match(str(ref))
    if not m:
        raise SignalboxError(f"'{ref}' is not a task: use its ID such as SB-12, or its issue number")
    return int(m.group(1))


def run(cmd, cwd=None, check=True, input=None, env=None):
    """Run a command; return stdout. Raises SignalboxError with stderr on failure when `check`."""
    try:
        p = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, text=True, env=env,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise SignalboxError(f"{cmd[0]} is not installed")
    if check and p.returncode != 0:
        raise SignalboxError(f"`{' '.join(cmd[:4])}` failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout if check else p


def git(*args, cwd=None, check=True, input=None):
    out = run(["git", *args], cwd=cwd, check=check, input=input)
    return out.strip() if check else out


def person_present():
    """Human decisions from the CLI need a person at a terminal; agents run without one."""
    forced = os.environ.get("SIGNALBOX_TEST_TTY")
    if forced in ("0", "1"):
        return forced == "1"
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Project and configuration
# ---------------------------------------------------------------------------


class Project:
    """The git repository a board belongs to. `root` is this worktree; `main` is the main worktree, where shared
    local state lives so every worktree of the repository sees the same cache, mirror and file hub."""

    def __init__(self, start=None):
        start = os.path.abspath(start or os.getcwd())
        try:
            self.root = git("rev-parse", "--show-toplevel", cwd=start)
            common = git("rev-parse", "--git-common-dir", cwd=start)  # relative to `start` before git 2.31's --path-format
            common = common if os.path.isabs(common) else os.path.join(start, common)
        except SignalboxError:
            raise SignalboxError("Signalbox works inside a git repository; run `git init` first, or cd into one")
        self.root = os.path.abspath(self.root)
        self.common = os.path.abspath(common)
        self.main = os.path.dirname(self.common) if os.path.basename(self.common) == ".git" else self.root
        self.config_path = self._find_config(start)
        self.config = load_config(self.config_path) if self.config_path else None

    def _find_config(self, start):
        d = start
        while True:
            p = os.path.join(d, STATE_DIR, CONFIG_FILE)
            if os.path.isfile(p):
                return p
            if os.path.abspath(d) == self.root or os.path.dirname(d) == d:
                break
            d = os.path.dirname(d)
        p = os.path.join(self.main, STATE_DIR, CONFIG_FILE)
        return p if os.path.isfile(p) else None

    @property
    def name(self):
        return os.path.basename(self.main.rstrip(os.sep)) or "project"

    def state_path(self, *parts):
        d = os.path.join(self.main, STATE_DIR)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, *parts)

    def require_config(self):
        if not self.config:
            raise SignalboxError(f"No {STATE_DIR}/{CONFIG_FILE} in this repository. Set Signalbox up with `{command('init')}`.")
        return self.config

    def remote(self):
        """The remote locks live on: the configured one, or origin when it exists. None means this machine only."""
        want = (self.config or DEFAULT_CONFIG)["locks"].get("remote", "auto")
        if want in (None, "", "local", False):
            return None
        remotes = git("remote", cwd=self.root, check=False).stdout.split()
        if want == "auto":
            return "origin" if "origin" in remotes else None
        if want not in remotes:
            raise SignalboxError(f"The lock remote '{want}' in {STATE_DIR}/{CONFIG_FILE} is not a git remote here")
        return want

    def github_repo(self):
        """owner/name of origin when it is on GitHub."""
        url = git("remote", "get-url", "origin", cwd=self.root, check=False).stdout.strip()
        m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
        return f"{m.group(1)}/{m.group(2)}" if m else None


def merge_defaults(cfg, defaults):
    out = json.loads(json.dumps(defaults))  # a deep copy: one hub's config must never change the defaults
    for k, v in cfg.items():
        out[k] = merge_defaults(v, defaults[k]) if isinstance(v, dict) and isinstance(defaults.get(k), dict) else v
    return out


def load_config(path):
    try:
        cfg = json.loads(K.read_text_file(path))
    except (OSError, ValueError) as e:
        raise SignalboxError(f"Cannot read {path}: {e}")
    if not isinstance(cfg, dict):
        raise SignalboxError(f"{path} must hold a JSON object")
    cfg = merge_defaults(cfg, DEFAULT_CONFIG)
    if not K.PREFIX_RE.match(str(cfg["prefix"])):
        raise SignalboxError(f"prefix '{cfg['prefix']}' in {path}: use 1-6 capital letters or digits, starting with a letter")
    return cfg


def command(rest=""):
    """How the person or agent reading a message runs Signalbox."""
    if INSTALLED or shutil.which("signalbox") and not os.path.basename(sys.argv[0]).endswith(".py"):
        base = "signalbox"
    else:
        base = "python .agents/skills/signalbox/signalbox.py"
    return f"{base} {rest}".strip()


# ---------------------------------------------------------------------------
# Trackers: where the shared board lives
# ---------------------------------------------------------------------------
#
# Both trackers speak in plain dicts shaped like GitHub's issues:
#   issue:   number, id, title, body, labels [names], state open|closed, state_reason, assignees [logins],
#            user, created_at, updated_at, url
#   comment: id, body, user, created_at, updated_at
#   label:   name, color, description


class FileTracker:
    """A GitHub-shaped issue store in one JSON file, for a single machine, offline work and tests. It lives in the
    main worktree's .signalbox/, so every worktree of the repository shares it."""

    kind = "file"

    def __init__(self, path, user=None):
        self.path = path
        self.user = user

    def describe(self):
        return f"file:{K.rel(self.path)}"

    def whoami(self):
        return self.user or default_user()

    def _empty(self):
        return {"next": 1, "issues": {}, "comments": {}, "labels": {}}

    def _read(self):
        if not os.path.isfile(self.path):
            return self._empty()
        try:
            data = json.loads(K.read_text_file(self.path))
        except ValueError as e:
            raise SignalboxError(f"{self.path} is damaged: {e}")
        return data

    def _tx(self, fn):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with K.locked(self.path):
            data = self._read()
            out = fn(data)
            K.write_text_atomic(self.path, json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        return out

    def _issue(self, data, number):
        issue = data["issues"].get(str(number))
        if not issue:
            raise SignalboxError(f"Issue #{number} not found in {self.describe()}")
        return issue

    @staticmethod
    def _touch(issue):
        issue["updated_at"] = iso(now())

    def list_issues(self, label=MARK):
        data = self._read()
        return [json.loads(json.dumps(i)) for i in sorted(data["issues"].values(), key=lambda i: i["number"])
                if label is None or label in i["labels"]]

    def get_issue(self, number):
        return json.loads(json.dumps(self._issue(self._read(), number)))

    def create_issue(self, title, body="", labels=(), assignees=()):
        def fn(data):
            n = data["next"]
            data["next"] = n + 1
            stamp = iso(now())
            issue = {"number": n, "id": 900000 + n, "title": title, "body": body, "labels": list(dict.fromkeys(labels)),
                     "state": "open", "state_reason": None, "assignees": list(assignees), "user": self.whoami(),
                     "created_at": stamp, "updated_at": stamp, "url": f"{self.describe()}#{n}"}
            data["issues"][str(n)] = issue
            return dict(issue)
        return self._tx(fn)

    def update_issue(self, number, **fields):
        def fn(data):
            issue = self._issue(data, number)
            for k, v in fields.items():
                if k in ("title", "body", "state", "state_reason"):
                    issue[k] = v
            if fields.get("state") == "open":
                issue["state_reason"] = None
            self._touch(issue)
            return dict(issue)
        return self._tx(fn)

    def add_labels(self, number, labels):
        def fn(data):
            issue = self._issue(data, number)
            issue["labels"] = list(dict.fromkeys(issue["labels"] + list(labels)))
            self._touch(issue)
        self._tx(fn)

    def remove_label(self, number, label):
        def fn(data):
            issue = self._issue(data, number)
            if label in issue["labels"]:
                issue["labels"].remove(label)
                self._touch(issue)
        self._tx(fn)

    def set_assignees(self, number, add=(), remove=()):
        def fn(data):
            issue = self._issue(data, number)
            keep = [a for a in issue["assignees"] if login_of(a) not in {login_of(r) for r in remove}]
            issue["assignees"] = list(dict.fromkeys(keep + [login_of(a) for a in add]))
            self._touch(issue)
        self._tx(fn)

    def list_comments(self, number):
        return [dict(c) for c in self._read()["comments"].get(str(number), [])]

    def add_comment(self, number, body):
        def fn(data):
            issue = self._issue(data, number)
            cid = sum(len(v) for v in data["comments"].values()) + 1 + 1000 * number
            stamp = iso(now())
            c = {"id": cid, "body": body, "user": self.whoami(), "created_at": stamp, "updated_at": stamp}
            data["comments"].setdefault(str(number), []).append(c)
            self._touch(issue)
            return dict(c)
        return self._tx(fn)

    def edit_comment(self, number, cid, body):
        def fn(data):
            for c in data["comments"].get(str(number), []):
                if c["id"] == cid:
                    c["body"], c["updated_at"] = body, iso(now())
                    self._touch(self._issue(data, number))
                    return
            raise SignalboxError(f"Comment {cid} not found on #{number}")
        self._tx(fn)

    def delete_comment(self, number, cid):
        def fn(data):
            data["comments"][str(number)] = [c for c in data["comments"].get(str(number), []) if c["id"] != cid]
            self._touch(self._issue(data, number))
        self._tx(fn)

    def list_labels(self):
        return [dict(name=k, **v) for k, v in self._read()["labels"].items()]

    def ensure_label(self, name, color="ededed", description=""):
        def fn(data):
            data["labels"].setdefault(name, {"color": color, "description": description})
        self._tx(fn)

    def update_label(self, name, new_name=None, description=None):
        def fn(data):
            lab = data["labels"].pop(name, {"color": "ededed", "description": ""})
            if description is not None:
                lab["description"] = description
            data["labels"][new_name or name] = lab
            if new_name and new_name != name:
                for issue in data["issues"].values():
                    issue["labels"] = [new_name if x == name else x for x in issue["labels"]]
        self._tx(fn)

    def delete_label(self, name):
        def fn(data):
            data["labels"].pop(name, None)
            for issue in data["issues"].values():
                if name in issue["labels"]:
                    issue["labels"].remove(name)
        self._tx(fn)

    # Native links: the file store has none beyond what Signalbox keeps in issue bodies.
    def link_sub_issue(self, parent, child):
        return False

    def link_blocked_by(self, number, blocker, on=True):
        return False

    def team_members(self, org, team):
        return None

    def create_pull(self, head, base, title, body):
        raise SignalboxError("Pull requests need a GitHub hub")


def default_user():
    user = os.environ.get("SIGNALBOX_USER")
    if user:
        return login_of(user)
    name = run(["git", "config", "user.name"], check=False).stdout.strip() if shutil.which("git") else ""
    return login_of(re.sub(r"\s+", "-", name)) or login_of(getpass.getuser())


class GitHubTracker:
    """GitHub Issues through the REST API. Token: $GH_TOKEN, $GITHUB_TOKEN, or `gh auth token`."""

    kind = "github"

    def __init__(self, repo, token=None, api="https://api.github.com", transport=None):
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo or ""):
            raise SignalboxError(f"GitHub repository '{repo}' should look like owner/name")
        self.repo, self.api = repo, api.rstrip("/")
        self._token = token
        self.transport = transport or self._urllib
        self.sleep = time.sleep
        self._me = None

    def describe(self):
        return f"github:{self.repo}"

    def token(self):
        if self._token is None:
            self._token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
            if not self._token and shutil.which("gh"):
                self._token = run(["gh", "auth", "token"], check=False).stdout.strip()
        return self._token

    def _urllib(self, method, url, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", f"signalbox/{VERSION}")
        if self.token():
            req.add_header("Authorization", f"Bearer {self.token()}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read()
                return res.status, json.loads(raw) if raw else None, dict(res.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = {"message": raw.decode("utf-8", "replace")[:200]}
            return e.code, payload, dict(e.headers or {})
        except urllib.error.URLError as e:
            raise SignalboxError(f"Cannot reach GitHub ({self.api}): {e.reason}")

    RETRIES = 3          # extra attempts after a rate limit or a server error
    MAX_WAIT = 60        # seconds; a rate limit that lasts longer fails at once and says until when

    @staticmethod
    def _header(headers, name):
        name = name.lower()
        return next((v for k, v in (headers or {}).items() if k.lower() == name), None)

    def _retry_after(self, method, status, payload, headers, attempt):
        """Seconds to wait before trying again, or None to give up. Rate limits (429, or 403 saying so) mean GitHub
        did nothing, so any request is retried; a server error may come after the write happened, so only requests
        that are safe to repeat are, never a POST that creates something."""
        msg = (payload or {}).get("message", "") if isinstance(payload, dict) else ""
        limited = status == 429 or (status == 403 and (self._header(headers, "Retry-After") is not None
                                                       or self._header(headers, "X-RateLimit-Remaining") == "0"
                                                       or "rate limit" in msg.lower()))
        if not (limited or (status in (500, 502, 503, 504) and method != "POST")):
            return None
        wait = None
        try:
            if self._header(headers, "Retry-After") is not None:
                wait = float(self._header(headers, "Retry-After"))
            elif limited and self._header(headers, "X-RateLimit-Remaining") == "0":
                wait = float(self._header(headers, "X-RateLimit-Reset")) - time.time() + 1
        except (TypeError, ValueError):
            wait = None
        if wait is None:
            wait = 2 ** attempt
        if wait > self.MAX_WAIT:
            until = time.strftime("%H:%M", time.localtime(time.time() + wait))
            raise SignalboxError(f"GitHub's rate limit refuses more requests until about {until}; try again then")
        return max(0.0, wait)

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        url = path if path.startswith("http") else f"{self.api}{path}"
        for attempt in range(self.RETRIES + 1):
            status, payload, headers = self.transport(method, url, body)
            if status in ok or attempt == self.RETRIES:
                break
            wait = self._retry_after(method, status, payload, headers, attempt)
            if wait is None:
                break
            self.sleep(wait)
        if status not in ok:
            msg = (payload or {}).get("message", "") if isinstance(payload, dict) else ""
            hint = " Set GH_TOKEN, or log in with `gh auth login`." if status in (401, 403) and not self.token() else ""
            raise SignalboxError(f"GitHub refused {method} {path.split('?')[0]}: {status} {msg}".rstrip() + (f".{hint}" if hint else ""))
        return payload, headers

    def pages(self, path):
        sep = "&" if "?" in path else "?"
        url, out = f"{path}{sep}per_page=100", []
        while url:
            payload, headers = self.call("GET", url)
            out.extend(payload or [])
            link = headers.get("Link") or headers.get("link") or ""
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else None
        return out

    @staticmethod
    def _issue(raw):
        return {"number": raw["number"], "id": raw.get("id"), "title": raw.get("title") or "", "body": raw.get("body") or "",
                "labels": [lab["name"] if isinstance(lab, dict) else lab for lab in raw.get("labels", [])],
                "state": raw.get("state", "open"), "state_reason": raw.get("state_reason"),
                "assignees": [a["login"].lower() for a in raw.get("assignees") or []],
                "user": ((raw.get("user") or {}).get("login") or "").lower(),
                "created_at": raw.get("created_at"), "updated_at": raw.get("updated_at"), "url": raw.get("html_url")}

    @staticmethod
    def _comment(raw):
        return {"id": raw["id"], "body": raw.get("body") or "", "user": ((raw.get("user") or {}).get("login") or "").lower(),
                "created_at": raw.get("created_at"), "updated_at": raw.get("updated_at")}

    def whoami(self):
        if self._me is None:
            override = os.environ.get("SIGNALBOX_USER")
            self._me = login_of(override) if override else login_of(self.call("GET", "/user")[0]["login"])
        return self._me

    def list_issues(self, label=MARK):
        q = f"/repos/{self.repo}/issues?state=all" + (f"&labels={quote(label)}" if label else "")
        return [self._issue(i) for i in self.pages(q) if "pull_request" not in i]

    def get_issue(self, number):
        return self._issue(self.call("GET", f"/repos/{self.repo}/issues/{number}")[0])

    def create_issue(self, title, body="", labels=(), assignees=()):
        payload = {"title": title, "body": body, "labels": list(labels)}
        if assignees:
            payload["assignees"] = list(assignees)
        return self._issue(self.call("POST", f"/repos/{self.repo}/issues", payload)[0])

    def update_issue(self, number, **fields):
        body = {k: v for k, v in fields.items() if k in ("title", "body", "state", "state_reason")}
        return self._issue(self.call("PATCH", f"/repos/{self.repo}/issues/{number}", body)[0])

    def add_labels(self, number, labels):
        self.call("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": list(labels)})

    def remove_label(self, number, label):
        self.call("DELETE", f"/repos/{self.repo}/issues/{number}/labels/{quote(label, safe='')}", ok=(200, 204, 404))

    def set_assignees(self, number, add=(), remove=()):
        # Assigning someone who cannot be assigned is ignored by GitHub, which suits a best-effort mirror.
        if add:
            self.call("POST", f"/repos/{self.repo}/issues/{number}/assignees", {"assignees": [login_of(a) for a in add]})
        if remove:
            self.call("DELETE", f"/repos/{self.repo}/issues/{number}/assignees", {"assignees": [login_of(a) for a in remove]})

    def list_comments(self, number):
        return [self._comment(c) for c in self.pages(f"/repos/{self.repo}/issues/{number}/comments")]

    def add_comment(self, number, body):
        return self._comment(self.call("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})[0])

    def edit_comment(self, number, cid, body):
        self.call("PATCH", f"/repos/{self.repo}/issues/comments/{cid}", {"body": body})

    def delete_comment(self, number, cid):
        self.call("DELETE", f"/repos/{self.repo}/issues/comments/{cid}", ok=(204, 404))

    def list_labels(self):
        return [{"name": lab["name"], "color": lab.get("color", ""), "description": lab.get("description") or ""}
                for lab in self.pages(f"/repos/{self.repo}/labels")]

    def ensure_label(self, name, color="ededed", description=""):
        self.call("POST", f"/repos/{self.repo}/labels", {"name": name, "color": color, "description": description[:100]},
                  ok=(201, 422))  # 422: it exists already

    def update_label(self, name, new_name=None, description=None):
        body = {}
        if new_name:
            body["new_name"] = new_name
        if description is not None:
            body["description"] = description[:100]
        self.call("PATCH", f"/repos/{self.repo}/labels/{quote(name, safe='')}", body)

    def delete_label(self, name):
        self.call("DELETE", f"/repos/{self.repo}/labels/{quote(name, safe='')}", ok=(204, 404))

    def link_sub_issue(self, parent, child):
        """Make `child` a native sub-issue of `parent`, so GitHub shows the map's tickets under it."""
        try:
            cid = self.get_issue(child)["id"]
            self.call("POST", f"/repos/{self.repo}/issues/{parent}/sub_issues", {"sub_issue_id": cid}, ok=(200, 201, 422))
            return True
        except SignalboxError:
            return False

    def link_blocked_by(self, number, blocker, on=True):
        """Mirror a dependency as GitHub's native "blocked by", which shows the frontier in GitHub's own UI."""
        try:
            bid = self.get_issue(blocker)["id"]
            if on:
                self.call("POST", f"/repos/{self.repo}/issues/{number}/dependencies/blocked_by", {"issue_id": bid},
                          ok=(200, 201, 422))
            else:
                self.call("DELETE", f"/repos/{self.repo}/issues/{number}/dependencies/blocked_by/{bid}", ok=(200, 204, 404))
            return True
        except SignalboxError:
            return False

    def team_members(self, org, team):
        try:
            return [m["login"].lower() for m in self.pages(f"/orgs/{org}/teams/{team}/members")]
        except SignalboxError:
            return None

    def create_pull(self, head, base, title, body):
        payload = {"head": head, "base": base, "title": title, "body": body}
        return self.call("POST", f"/repos/{self.repo}/pulls", payload)[0].get("html_url")


def make_tracker(project, config):
    hub = config["hub"]
    if hub.get("type") == "github":
        return GitHubTracker(hub.get("repo") or project.github_repo() or "", api=hub.get("api", "https://api.github.com"))
    if hub.get("type") == "file":
        path = hub.get("path") or ".signalbox/hub.json"
        return FileTracker(path if os.path.isabs(path) else os.path.join(project.main, path))
    raise SignalboxError(f"Unknown hub type '{hub.get('type')}' (use github or file)")


# ---------------------------------------------------------------------------
# Interlocks: atomic task locks held in git
# ---------------------------------------------------------------------------
#
# A task's lock is a ref, refs/signalbox/locks/<issue number>, pointing at a commit whose message is the lock record
# (who holds it, for whom, where, until when). The ref is append-only: claiming, renewing and releasing each add a
# commit on top of the current one (releasing adds a "free" record), and each push names the commit it expects to
# replace (`git push --force-with-lease=<ref>:<expected>`), so of two racing claims exactly one wins. Nothing is ever
# deleted or force-pushed, which keeps interlocks working on remotes and proxies that forbid both, as protected
# branches often do. Without a remote, `git update-ref <ref> <new> <old>` does the same on the repository's own refs,
# which every worktree of the repository shares, so agents on one machine contend on the same locks too.

ZERO = "0" * 40
SEEN = "refs/signalbox/seen"
GIT_ID = {"GIT_AUTHOR_NAME": "signalbox", "GIT_AUTHOR_EMAIL": "signalbox@localhost",
          "GIT_COMMITTER_NAME": "signalbox", "GIT_COMMITTER_EMAIL": "signalbox@localhost"}
BRANCH_LOCKS = "refs/heads/sb-lock"  # for remotes that accept nothing but branches


class Interlock:
    def __init__(self, project, remote, namespace="refs/signalbox/locks"):
        self.cwd, self.remote, self.ns = project.root, remote, namespace.rstrip("/")
        self._raw = None

    def where(self):
        return f"remote '{self.remote}'" if self.remote else "this repository only"

    def ref(self, n):
        return f"{self.ns}/{n}"

    def _commit(self, record, parent=None):
        tree = git("mktree", cwd=self.cwd, input="")
        title = f"signalbox {'free' if record.get('free') else 'lock'} {record.get('task', '')}"
        msg = f"{title}\n\n{json.dumps(record, sort_keys=True, indent=1)}\n"
        args = ["git", "commit-tree", tree] + (["-p", parent] if parent else []) + ["-F", "-"]
        return run(args, cwd=self.cwd, input=msg, env={**os.environ, **GIT_ID}).strip()

    @staticmethod
    def _record(message):
        try:
            return json.loads(message.split("\n\n", 1)[1])
        except (IndexError, ValueError):
            return {}

    def _local_table(self, prefix):
        out = git("for-each-ref", "--format=%(refname)%00%(objectname)%00%(contents)%01", prefix + "/", cwd=self.cwd)
        table = {}
        for chunk in out.split("\x01"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            ref, sha, message = (chunk.split("\x00") + ["", ""])[:3]
            tail = ref.rsplit("/", 1)[-1]
            if tail.isdigit():
                table[int(tail)] = (sha, self._record(message))
        return table

    def raw(self, refresh=False):
        """{issue number: (sha, record)} of every lock ref, free ones included."""
        if self._raw is not None and not refresh:
            return self._raw
        if self.remote:
            p = git("fetch", "--quiet", "--no-tags", "--prune", self.remote, f"+{self.ns}/*:{SEEN}/*", cwd=self.cwd, check=False)
            if p.returncode != 0:
                raise SignalboxError(f"Cannot read the locks on {self.where()}: {p.stderr.strip()}")
            self._raw = self._local_table(SEEN)
        else:
            self._raw = self._local_table(self.ns)
        return self._raw

    def table(self, refresh=False):
        """{issue number: (sha, record)} of every lock held now."""
        return {n: v for n, v in self.raw(refresh).items() if not v[1].get("free")}

    def get(self, n, refresh=True):
        return self.table(refresh).get(n, (None, None))

    def _cas(self, n, new, old):
        """Point the lock ref of `n` at `new` if it is still `old` (None: must not exist yet)."""
        ref = self.ref(n)
        if self.remote:
            lease = f"--force-with-lease={ref}:{old or ''}"
            p = git("push", "--quiet", "--no-verify", self.remote, lease, f"{new}:{ref}", cwd=self.cwd, check=False)
            if p.returncode == 0:
                git("update-ref", f"{SEEN}/{n}", new, cwd=self.cwd)
                self._raw = None
                return True
            text = (p.stderr or "") + (p.stdout or "")
            if self._lost_race(ref, old, text):
                return False
            hint = f"\nTaking a lock means pushing to {self.remote}: check that you have write access to it."
            if not self.ns.startswith("refs/heads/"):
                hint += (f" If it only accepts branches, set \"namespace\": \"{BRANCH_LOCKS}\" under \"locks\" "
                         f"in {STATE_DIR}/{CONFIG_FILE}.")
            raise SignalboxError(f"Cannot change the lock on {self.where()}: {text.strip()}{hint}")
        ok = git("update-ref", ref, new, old or ZERO, cwd=self.cwd, check=False).returncode == 0
        self._raw = None
        return ok

    # What git prints when the ref was not what the lease expected, or another push held it at that moment.
    RACE_SIGNS = ("stale info", "fetch first", "already exists", "failed to lock", "cannot lock ref",
                  "failed to update ref", "incorrect old value")

    def _lost_race(self, ref, old, text):
        """Whether a refused push lost to another writer, rather than being refused outright (a hook, missing rights,
        a remote that accepts only branches). Losing means the ref no longer is what we expected."""
        if any(sign in text for sign in self.RACE_SIGNS):
            return True
        p = git("ls-remote", self.remote, ref, cwd=self.cwd, check=False)
        if p.returncode != 0:
            return False
        now_at = next((line.split("\t")[0] for line in p.stdout.splitlines() if line.endswith("\t" + ref)), "")
        return now_at != (old or "")

    def _winner(self, n, task):
        cur_sha, cur = self.get(n)
        holder = (cur or {}).get("label") or (cur or {}).get("holder") or "someone"
        return Locked(f"{task} is already locked by {holder}", cur, cur_sha)

    def acquire(self, n, record):
        sha, cur = self.raw(refresh=True).get(n, (None, None))
        if sha and not cur.get("free"):
            raise self._winner(n, record.get("task", n))
        new = self._commit(record, parent=sha)
        if self._cas(n, new, sha):
            return new
        raise self._winner(n, record.get("task", n))

    def replace(self, n, old, record):
        known = git("cat-file", "-e", f"{old}^{{commit}}", cwd=self.cwd, check=False).returncode == 0
        new = self._commit(record, parent=old) if known else None
        if new and self._cas(n, new, old):
            return new
        cur_sha, cur = self.get(n)
        raise Locked(f"The lock on {record.get('task', n)} changed meanwhile; it is now held by "
                     f"{(cur or {}).get('label') or 'nobody'}", cur, cur_sha)

    def release(self, n, old, by=None):
        record = {"free": True, "task": f"#{n}", "released": iso(now())}
        if by:
            record["by"] = by
        new = self._commit(record, parent=old)
        if not self._cas(n, new, old):
            cur_sha, cur = self.get(n)
            if cur_sha:
                raise Locked(f"The lock on #{n} changed meanwhile; it is now held by {cur.get('label')}", cur, cur_sha)


def lease_left(record, at=None):
    end = parse_iso((record or {}).get("expires"))
    return (end - (at or now())).total_seconds() if end else 0


def lock_line(record):
    left = lease_left(record)
    where = f" · {record.get('branch')}" if record.get("branch") else ""
    host = f" on {record['host']}" if record.get("host") else ""
    lease = f"lease {span(left)} left" if left > 0 else f"lease expired {span(left)} ago, can be taken over"
    pair = f" with @{record['pair']}" if record.get("pair") else ""
    return f"{record.get('label') or record.get('holder')}{pair}{host}{where} · {lease}"


# ---------------------------------------------------------------------------
# Worktrees: one isolated checkout per claimed task
# ---------------------------------------------------------------------------
#
# Plain `git worktree` gives isolation and nothing else. Signalbox adds what a team needs around it: the worktree is
# created when the lock is won and `git worktree lock`ed with the holder in its reason, untracked files such as .env
# are copied in, each task gets its own port, a TASK.md brief tells the agent what it holds, the branch is pushed so
# another person or machine can resume it, and `gc` removes it once the task is done.


class Worktrees:
    def __init__(self, project, config):
        self.project, self.cfg = project, config["worktrees"]

    def dir(self):
        d = self.cfg.get("dir") or "../{repo}.worktrees"
        d = d.replace("{repo}", self.project.name)
        return os.path.normpath(d if os.path.isabs(d) else os.path.join(self.project.main, d))

    def path_for(self, task_id):
        return os.path.join(self.dir(), task_id)

    @staticmethod
    def branch_for(task_id, title):
        return f"sb/{task_id.lower()}-{slug(title, 32)}"

    def list(self):
        out = git("worktree", "list", "--porcelain", cwd=self.project.root)
        items, cur = [], None
        for line in out.splitlines() + [""]:
            if line.startswith("worktree "):
                cur = {"path": os.path.normpath(line[9:]), "branch": None, "locked": None}
            elif cur is None:
                continue
            elif line.startswith("branch "):
                cur["branch"] = line[7:].replace("refs/heads/", "", 1)
            elif line.startswith("locked"):
                cur["locked"] = line[7:] or "locked"
            elif line.startswith("prunable"):
                cur["prunable"] = True
            elif not line:
                items.append(cur)
                cur = None
        return items

    def find(self, task_id):
        want = os.path.normcase(self.path_for(task_id))
        pre = f"sb/{task_id.lower()}-"
        for w in self.list():
            if os.path.normcase(w["path"]) == want or (w["branch"] or "").startswith(pre):
                return w
        return None

    def find_branch(self, task_id, remote):
        pre = f"sb/{task_id.lower()}-"
        local = git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{pre}*", cwd=self.project.root).split()
        if local:
            return local[0], "local"
        if remote:
            p = git("ls-remote", "--heads", remote, f"{pre}*", cwd=self.project.root, check=False)
            for line in p.stdout.splitlines():
                ref = line.split("\t")[-1].strip()
                if ref.startswith("refs/heads/"):
                    return ref[11:], "remote"
        return None, None

    def base(self, remote):
        if self.cfg.get("base"):
            return self.cfg["base"]
        if remote:
            head = git("symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD", cwd=self.project.root, check=False)
            if head.returncode == 0 and head.stdout.strip():
                return head.stdout.strip().replace("refs/remotes/", "", 1)
        return git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.project.main, check=False).stdout.strip() or "HEAD"

    def create(self, task_id, title, remote):
        """Return (path, branch, how): how is new, resumed (from a pushed branch) or existing."""
        found = self.find(task_id)
        if found and os.path.isdir(found["path"]):
            return found["path"], found["branch"], "existing"
        path = self.path_for(task_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        branch, where = self.find_branch(task_id, remote)
        root = self.project.root
        if where == "local":
            git("worktree", "add", "--quiet", path, branch, cwd=root)
            return path, branch, "resumed"
        if where == "remote":
            git("fetch", "--quiet", remote, f"refs/heads/{branch}:refs/heads/{branch}", cwd=root)
            git("worktree", "add", "--quiet", path, branch, cwd=root)
            git("branch", "--quiet", "--set-upstream-to", f"{remote}/{branch}", branch, cwd=root, check=False)
            return path, branch, "resumed"
        branch = self.branch_for(task_id, title)
        base = self.base(remote)
        if remote and base.startswith(remote + "/"):
            git("fetch", "--quiet", remote, base[len(remote) + 1:], cwd=root, check=False)
        git("worktree", "add", "--quiet", "-b", branch, path, base, cwd=root)
        return path, branch, "new"

    def lock(self, path, reason):
        git("worktree", "unlock", path, cwd=self.project.root, check=False)
        git("worktree", "lock", "--reason", reason, path, cwd=self.project.root, check=False)

    def unlock(self, path):
        git("worktree", "unlock", path, cwd=self.project.root, check=False)

    def dirty(self, path):
        if not os.path.isdir(path):
            return []
        out = git("status", "--porcelain", cwd=path, check=False).stdout
        return [line[3:] for line in out.splitlines() if line[3:] and not line[3:].startswith(STATE_DIR + "/")]

    def remove(self, path, force=False):
        self.unlock(path)
        args = ["worktree", "remove"] + (["--force"] if force else []) + [path]
        git(*args, cwd=self.project.root)

    def port_for(self, number):
        return int(self.cfg.get("port_base") or 4100) + number % 1000

    def bootstrap(self, path, env, brief):
        """Copy untracked files in, write the brief and env file, and run the setup command. Returns warnings."""
        warnings = []
        for pattern in self.cfg.get("copy") or []:
            for src in sorted(_glob(self.project.main, pattern)):
                rel_path = os.path.relpath(src, self.project.main)
                dst = os.path.join(path, rel_path)
                if os.path.isfile(src) and not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
        state = os.path.join(path, STATE_DIR)
        os.makedirs(state, exist_ok=True)
        K.write_text_atomic(os.path.join(state, "TASK.md"), brief)
        K.write_text_atomic(os.path.join(state, "env"), "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()))
        setup = (self.cfg.get("setup") or "").strip()
        if setup:
            p = subprocess.run(setup, shell=True, cwd=path, env={**os.environ, **{k: str(v) for k, v in env.items()}},
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            if p.returncode != 0:
                tail = "\n".join((p.stderr or p.stdout).strip().splitlines()[-5:])
                warnings.append(f"The worktree setup command `{setup}` failed (exit {p.returncode}):\n{tail}")
        return warnings


def _glob(root, pattern):
    import glob as globmod
    return globmod.glob(os.path.join(root, pattern), recursive=True)


def changed_files(project, branch, base, remote, worktree=None):
    """Files a task's branch changes against its base, plus uncommitted changes in its worktree."""
    files = set()
    root = project.root
    ref = None
    for cand in (branch, f"{remote}/{branch}" if remote else None):
        if cand and git("rev-parse", "--verify", "--quiet", cand, cwd=root, check=False).returncode == 0:
            ref = cand
            break
    if ref:
        mb = git("merge-base", base, ref, cwd=root, check=False).stdout.strip()
        if mb:
            files.update(git("diff", "--name-only", mb, ref, cwd=root, check=False).stdout.split())
    if worktree and os.path.isdir(worktree):
        for line in git("status", "--porcelain", cwd=worktree, check=False).stdout.splitlines():
            name = line[3:].split(" -> ")[-1]
            if name and not name.startswith(STATE_DIR + "/"):
                files.add(name)
    return files


def paths_overlap(a, b):
    """Whether two declared path patterns can cover the same file (src/** and src/api/x.py do)."""
    a, b = a.rstrip("/"), b.rstrip("/")
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    def stem(p):
        return re.split(r"[*?\[]", p, 1)[0].rstrip("/")
    sa, sb = stem(a), stem(b)
    return bool(sa and sb) and (sa.startswith(sb + "/") or sb.startswith(sa + "/") or sa == sb)


# ---------------------------------------------------------------------------
# Actors: who is acting, and for whom
# ---------------------------------------------------------------------------


class Actor:
    """A person (`@login`) or an agent (`claude-code`) acting for a person, its sponsor."""

    def __init__(self, name, human, sponsor=None):
        self.name, self.human, self.sponsor = name, human, sponsor

    @property
    def by(self):
        return f"@{self.name}" if self.human else self.name

    @property
    def label(self):
        return self.by if self.human or not self.sponsor else f"{self.name} (for @{self.sponsor})"

    @property
    def login(self):
        return self.name if self.human else self.sponsor


def safe_whoami(tracker):
    try:
        return tracker.whoami()
    except SignalboxError:
        return default_user()


def resolve_actor(args, tracker, human=None):
    agent = None if human else (getattr(args, "as_name", None) or os.environ.get("SIGNALBOX_AGENT")
                                or os.environ.get("KANVAS_AGENT"))
    if agent:
        name = re.sub(r"[^\w.-]+", "-", agent.strip()).strip("-").lower() or "agent"
        sponsor = login_of(os.environ.get("SIGNALBOX_SPONSOR", "")) or safe_whoami(tracker)
        return Actor(name, False, sponsor)
    return Actor(safe_whoami(tracker) or "someone", True)


def render_note(text, actor, kind="note"):
    head = {"by": actor.by, "kind": kind}
    if not actor.human and actor.sponsor:
        head["for"] = actor.sponsor
    title = {"note": "", "summary": " · finished", "question": " · asks", "event": "", "resolution": " · resolution",
             "feedback": " · sent back", "changes": " · changes requested"}.get(kind, f" · {kind}")
    return f"<!-- signalbox:note {dumps_meta(head)} -->\n**{actor.label}**{title}\n\n{text.strip()}"


def parse_note(comment):
    body = (comment.get("body") or "").replace("\r\n", "\n")
    m = NOTE_RE.match(body)
    if m:
        try:
            head = json.loads(m.group(1))
        except ValueError:
            head = {}
        rest = body[m.end():]
        if rest.startswith("**"):
            rest = rest.split("\n", 1)[1] if "\n" in rest else ""
        by, text, kind = head.get("by") or f"@{comment.get('user')}", rest.strip(), head.get("kind", "note")
    else:
        by, text, kind = f"@{comment.get('user') or 'someone'}", body.strip(), "note"
    note = {"at": comment.get("created_at"), "by": by, "text": text, "cid": comment["id"]}
    if kind != "note":
        note["kind"] = kind
    c, u = parse_iso(comment.get("created_at")), parse_iso(comment.get("updated_at"))
    if c and u and (u - c).total_seconds() > 2:
        note["edited"] = comment["updated_at"]
    return note


# ---------------------------------------------------------------------------
# Map bodies: Wayfinder's sections in an issue
# ---------------------------------------------------------------------------


def map_template(destination, notes=""):
    parts = []
    for name in MAP_SECTIONS:
        content = destination if name == "Destination" else notes if name == "Notes" else ""
        parts.append(f"## {name}\n\n{content.strip()}".rstrip())
    return "\n\n".join(parts)


def sections(text):
    """[(heading or None, content)] of Markdown split at `## ` headings."""
    out, head, buf = [], None, []
    for line in (text or "").split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            out.append((head, "\n".join(buf).strip()))
            head, buf = m.group(1), []
        else:
            buf.append(line)
    out.append((head, "\n".join(buf).strip()))
    return [(h, c) for h, c in out if h is not None or c]


def section(text, name):
    for h, c in sections(text):
        if h and h.lower() == name.lower():
            return c
    return ""


def set_section(text, name, content):
    parts, done = sections(text), False
    for i, (h, _) in enumerate(parts):
        if h and h.lower() == name.lower():
            parts[i], done = (h, content.strip()), True
    if not done:
        parts.append((name, content.strip()))
    return "\n\n".join((f"## {h}\n\n{c}".rstrip() if h else c) for h, c in parts)


def bullets(content):
    return [line[2:].strip() for line in content.split("\n") if line.startswith("- ")]


# ---------------------------------------------------------------------------
# The hub: the shared board, its rules, and every move on it
# ---------------------------------------------------------------------------


class Hub:
    def __init__(self, project, tracker=None, actor=None, interactive=False):
        self.project = project
        self.config = project.require_config()
        self.tracker = tracker or make_tracker(project, self.config)
        self.actor = actor or Actor(safe_whoami(self.tracker), True)
        self.interactive = interactive
        self.prefix = self.config["prefix"]
        self.locks = Interlock(project, project.remote(), self.config["locks"].get("namespace") or "refs/signalbox/locks")
        self.worktrees = Worktrees(project, self.config)
        self.warnings = []
        self._issues = self._labels = None
        self._comments = {}
        # Issues this process created or fetched. GitHub's issue listing lags a few seconds behind writes, so these
        # win over a listing that does not have them yet, or has an older copy.
        self._recent = {}

    # -- reading ---------------------------------------------------------------

    def tid(self, n):
        return f"{self.prefix}-{n}"

    def invalidate(self):
        """Forget the issue listing after a write. Labels stay: only lane changes touch them, and those reset them."""
        self._issues = None

    def issues(self):
        if self._issues is None:
            listed = {i["number"]: i for i in self.tracker.list_issues(MARK)}
            for n, i in self._recent.items():
                if n not in listed or (listed[n].get("updated_at") or "") < (i.get("updated_at") or ""):
                    listed[n] = i
            self._issues = listed
        return self._issues

    def remember(self, issue):
        self._recent[issue["number"]] = issue
        if self._issues is not None:
            self._issues[issue["number"]] = issue
        return issue

    def labels(self):
        if self._labels is None:
            self._labels = self.tracker.list_labels()
        return self._labels

    def issue(self, ref, fresh=True):
        n = number_of(ref)
        if fresh:
            try:
                issue = self.tracker.get_issue(n)
            except SignalboxError:
                raise SignalboxError(f"Task {self.tid(n)} not found on {self.tracker.describe()}")
            if MARK not in issue["labels"]:
                raise SignalboxError(f"#{n} is not a Signalbox task (it has no '{MARK}' label)")
            self.issues()
            return self.remember(issue)
        issue = self.issues().get(n)
        if not issue:
            raise SignalboxError(f"Task {self.tid(n)} not found on {self.tracker.describe()}")
        return issue

    def comments(self, issue, save=True):
        """The issue's comments, re-read only when the issue changed. `save=False` leaves writing the disk cache to
        the caller, which reads many issues and writes it once (`_save_disk_cache`)."""
        n = issue["number"]
        cached = self._comments.get(n)
        if cached and cached[0] == issue.get("updated_at"):
            return cached[1]
        disk = self._disk_cache() if self.tracker.kind == "github" else {}
        hit = disk.get(str(n))
        if hit and hit.get("updated_at") == issue.get("updated_at"):
            comments = hit["comments"]
        else:
            comments = self.tracker.list_comments(n)
            if self.tracker.kind == "github":
                disk[str(n)] = {"updated_at": issue.get("updated_at"), "comments": comments}
                self._disk_dirty = True
                if save:
                    self._save_disk_cache()
        self._comments[n] = (issue.get("updated_at"), comments)
        return comments

    def _disk_cache(self):
        if not hasattr(self, "_disk"):
            try:
                self._disk = json.loads(K.read_text_file(self.project.state_path("cache.json")))
            except (OSError, ValueError):
                self._disk = {}
        return self._disk

    def _save_disk_cache(self, keep=None):
        """Write the comment cache if it changed. `keep`, a set of issue numbers, drops every other entry, so issues
        that were closed do not stay in the cache for ever."""
        disk = self._disk_cache()
        if keep is not None:
            for key in [k for k in disk if not (k.isdigit() and int(k) in keep)]:
                del disk[key]
                self._disk_dirty = True
        if not getattr(self, "_disk_dirty", False):
            return
        try:
            K.write_text_atomic(self.project.state_path("cache.json"), json.dumps(disk, ensure_ascii=False))
            self._disk_dirty = False
        except OSError:
            pass

    @staticmethod
    def kind_of(issue):
        for lab in issue["labels"]:
            if lab.startswith(KIND_PREFIX) and lab[len(KIND_PREFIX):] in KINDS:
                return lab[len(KIND_PREFIX):]
        return split_body(issue["body"])[1].get("kind") or "build"

    @staticmethod
    def state_of(issue):
        """Board state of an issue; None for one closed as not planned (withdrawn or rejected)."""
        if issue["state"] == "closed":
            return None if issue.get("state_reason") in ("not_planned", "duplicate") else "done"
        present = [LABEL_STATES[lab] for lab in issue["labels"] if lab in LABEL_STATES]
        return max(present, key=K.STATES.index) if present else "todo"

    def lanes(self):
        """[{id, name, label, owners, wip}]: lanes from the config first, then lane labels found on the hub."""
        out, taken = [], {self.prefix}
        for lane in self.config["lanes"]:
            name = " ".join(str(lane.get("name", "")).split())
            if not name:
                continue
            pid = (lane.get("prefix") or lane.get("id") or K.suggest_prefix(name, taken)).upper()
            taken.add(pid)
            out.append({"id": pid, "name": name, "label": LANE_PREFIX + name, "owners": list(lane.get("owners") or []),
                        "wip": lane.get("wip"), "configured": True})
        known = {lane["label"].lower() for lane in out}
        try:
            labels = self.labels()
        except SignalboxError:
            labels = []
        for lab in sorted(labels, key=lambda x: x["name"].lower()):
            if not lab["name"].startswith(LANE_PREFIX) or lab["name"].lower() in known:
                continue
            desc = lab.get("description") or ""
            m = re.search(r"\[([A-Z][A-Z0-9]{0,5})\]", desc)
            pid = m.group(1) if m and m.group(1) not in taken else K.suggest_prefix(lab["name"][len(LANE_PREFIX):], taken)
            taken.add(pid)
            owners = re.search(r"owners:\s*([^·]+)", desc)
            wip = re.search(r"wip\s*(\d+)", desc)
            out.append({"id": pid, "name": lab["name"][len(LANE_PREFIX):], "label": lab["name"],
                        "owners": [o.strip() for o in owners.group(1).split(",") if o.strip()] if owners else [],
                        "wip": int(wip.group(1)) if wip else None, "configured": False})
        return out

    def lane(self, ref):
        if ref in (None, "", "-"):
            return None
        key = str(ref).strip().lower()
        for lane in self.lanes():
            if key in (lane["id"].lower(), lane["name"].lower(), lane["label"].lower()):
                return lane
        names = ", ".join(f"{x['name']} [{x['id']}]" for x in self.lanes()) or "none yet"
        raise SignalboxError(f"Lane '{ref}' not found (lanes: {names}). Create it with `{command('add-lane NAME')}`.")

    def lane_of(self, issue):
        for lane in self.lanes():
            if lane["label"] in issue["labels"]:
                return lane
        return None

    def lock_table(self, refresh=False):
        try:
            return self.locks.table(refresh)
        except SignalboxError as e:
            self.warnings.append(str(e))
            return {}

    def board(self):
        """The whole hub as a Kanvas board, with each task's issue, kind and lock under `sb`."""
        issues = self.issues()
        locks = self.lock_table()
        lanes = self.lanes()
        live = {n: i for n, i in issues.items() if self.state_of(i) is not None}
        maps = {n: i for n, i in live.items() if self.kind_of(i) == "map"}
        groups = [{"id": lane["id"], "name": lane["name"]} for lane in lanes]
        groups += [{"id": f"M{n}", "name": f"Map: {m['title']}"} for n, m in sorted(maps.items())]
        tasks = []
        for n, issue in sorted(live.items()):
            desc, meta = split_body(issue["body"])
            kind = self.kind_of(issue)
            lane = self.lane_of(issue)
            parent = meta.get("map")
            group = f"M{n}" if kind == "map" else f"M{parent}" if parent in maps else lane["id"] if lane else None
            deps = [self.tid(d) for d in meta.get("deps", []) if isinstance(d, int) and d in live]
            notes = [parse_note(c) for c in self.comments(issue, save=False)]
            sb = {"issue": n, "url": issue.get("url"), "kind": kind}
            if kind in HITL_KINDS:
                sb["hitl"] = True
            if lane:
                sb["lane"] = lane["name"]
            if parent:
                sb["map"] = parent
            if meta.get("paths"):
                sb["paths"] = meta["paths"]
            if issue.get("assignees"):
                sb["assignees"] = issue["assignees"]
            if n in locks:
                sha, rec = locks[n]
                sb["lock"] = {k: rec.get(k) for k in ("label", "holder", "sponsor", "pair", "host", "branch", "worktree",
                                                       "acquired", "expires") if rec.get(k)}
                if lease_left(rec) <= 0:
                    sb["lock"]["expired"] = True
            if kind == "map":
                sb["destination"] = section(desc, "Destination")
                sb["fog"] = len(bullets(section(desc, "Not yet specified")))
            task = {"id": self.tid(n), "title": issue["title"], "state": self.state_of(issue), "group": group,
                    "deps": deps, "at": meta.get("at") or issue.get("created_at"),
                    "by": meta.get("by") or f"@{issue.get('user') or 'someone'}", "desc": desc, "notes": notes, "sb": sb}
            if YOLO_LABEL in issue["labels"]:
                task["yolo"] = True
            tasks.append(task)
        if self.tracker.kind == "github":
            self._save_disk_cache(keep=set(live))
        hub = {"hub": self.tracker.describe(), "me": self.actor.by, "locks": self.locks.where(), "version": VERSION}
        return {"kanvas": K.FORMAT, "title": self.config.get("title") or self.project.name, "groups": groups,
                "tasks": tasks, "signalbox": hub}

    def task(self, ref, board=None):
        board = board or self.board()
        t = K.task_map(board).get(self.tid(number_of(ref)).upper())
        if not t:
            raise SignalboxError(f"Task {ref} is not on the board (it may be withdrawn or rejected)")
        return board, t

    # -- writing helpers -------------------------------------------------------

    def ensure_labels(self):
        have = {lab["name"] for lab in self.labels()}
        for name, color in LABEL_COLORS.items():
            if name not in have:
                self.tracker.ensure_label(name, color, LABEL_HELP.get(name, ""))
        for kind in KINDS[1:]:
            if KIND_PREFIX + kind not in have:
                mode = "HITL" if kind in HITL_KINDS else "AFK" if kind == "research" else ""
                self.tracker.ensure_label(KIND_PREFIX + kind, "0ea5e9", f"Signalbox {kind} ticket {mode}".strip())
        for lane in self.lanes():
            if lane["label"] not in have:
                self.tracker.ensure_label(lane["label"], "14b8a6", lane_description(lane))
        self._labels = None

    def note(self, n, text, kind="note"):
        comment = self.tracker.add_comment(n, render_note(text, self.actor, kind))
        self._comments.pop(n, None)
        self.invalidate()
        return comment

    def set_state(self, issue, state):
        """Move an issue to a board state through labels, closing and reopening."""
        n = issue["number"]
        if state in ("done", "rejected"):
            self.tracker.update_issue(n, state="closed", state_reason="completed" if state == "done" else "not_planned")
        else:
            if issue["state"] == "closed":
                self.tracker.update_issue(n, state="open")
            self.tracker.add_labels(n, [STATE_LABELS[state]])
        for lab in issue["labels"]:
            if lab in LABEL_STATES and (state in ("done", "rejected") or LABEL_STATES[lab] != state):
                self.tracker.remove_label(n, lab)
        self.invalidate()
        self.remember(self.tracker.get_issue(n))

    def update_meta(self, issue, fn, desc=None):
        old_desc, meta = split_body(issue["body"])
        fn(meta)
        body = join_body(old_desc if desc is None else desc, meta)
        if body != issue["body"]:
            issue = self.remember(self.tracker.update_issue(issue["number"], body=body))
            self.invalidate()
        return issue

    def require_human(self, what):
        if not self.actor.human:
            raise SignalboxError(f"Only a person can {what}. Ask them in the conversation; they decide in the board "
                                 f"(`{command('serve')}`) or in their own terminal.")
        if not self.interactive:
            raise SignalboxError(f"To {what} is a human decision: run this in your own terminal, or use the board "
                                 f"(`{command('serve')}`). Commands run without a person at the terminal cannot make it.")

    def approvers_for(self, issue):
        lane = self.lane_of(issue)
        names = list(self.config.get("approvers") or []) + (lane["owners"] if lane else [])
        logins = set()
        for name in names:
            name = str(name).strip()
            m = re.match(r"^@?([\w.-]+)/([\w.-]+)$", name)
            if m:
                members = self.config.get("teams", {}).get(f"{m.group(1)}/{m.group(2)}")
                if members is None:
                    members = self.tracker.team_members(m.group(1), m.group(2)) or []
                logins.update(login_of(x) for x in members)
            elif name.startswith("team:"):
                logins.update(login_of(x) for x in self.config.get("teams", {}).get(name[5:], []))
            else:
                logins.add(login_of(name))
        return logins

    def require_approver(self, issue, what):
        self.require_human(what)
        allowed = self.approvers_for(issue)
        if allowed and login_of(self.actor.login) not in allowed:
            lane = self.lane_of(issue)
            where = f" in lane {lane['name']}" if lane else ""
            raise SignalboxError(f"@{self.actor.login} cannot {what} {self.tid(issue['number'])}{where}: that is up to "
                                 f"{', '.join('@' + x for x in sorted(allowed))}.")

    def _check_deps(self, n, deps):
        board = self.board()
        tasks = K.task_map(board)
        for d in deps:
            if d == n:
                raise SignalboxError(f"{self.tid(n)} cannot depend on itself")
            if self.tid(d).upper() not in tasks:
                raise SignalboxError(f"Dependency {self.tid(d)} not found")
            if K.depends_on(board, self.tid(d), self.tid(n)):
                raise SignalboxError(f"{self.tid(n)} -> {self.tid(d)} would create a cycle: {self.tid(d)} already "
                                     f"depends on {self.tid(n)}")

    def _link_deps(self, n, deps, on=True):
        for d in deps:
            self.tracker.link_blocked_by(n, d, on)

    # -- proposing and planning -----------------------------------------------

    def propose(self, title, desc="", lane=None, kind="build", deps=(), map_ref=None, paths=(), state=None):
        title = " ".join(str(title or "").split())
        if not title:
            raise SignalboxError("Task title is empty")
        if kind not in KINDS:
            raise SignalboxError(f"Unknown kind '{kind}' (use one of {', '.join(KINDS)})")
        lane = self.lane(lane) if isinstance(lane, str) or lane is None else lane
        dep_numbers = []
        live = self.issues()

        def known(n):
            if n not in live:
                try:
                    self.issue(n)
                except SignalboxError:
                    return None
            return self.issues().get(n)
        for ref in deps or ():
            d = number_of(ref)
            if not known(d) or self.state_of(known(d)) is None:
                raise SignalboxError(f"Dependency '{ref}' not found")
            dep_numbers.append(d)
        parent = None
        if map_ref is not None:
            parent = number_of(map_ref)
            if not known(parent) or self.kind_of(known(parent)) != "map":
                raise SignalboxError(f"{self.tid(parent)} is not a map; create one with `{command('map new')}`")
        state = state or ("proposed" if not self.actor.human else "todo")
        meta = {"by": self.actor.by, "at": iso(now()), "deps": sorted(set(dep_numbers)), "map": parent,
                "paths": list(paths or [])}
        labels = [MARK, STATE_LABELS[state]] + ([lane["label"]] if lane else []) + ([KIND_PREFIX + kind] if kind != "build" else [])
        issue = self.remember(self.tracker.create_issue(title, join_body(desc, meta), labels))
        n = issue["number"]
        self._link_deps(n, dep_numbers)
        if parent:
            self.tracker.link_sub_issue(parent, n)
        self.invalidate()
        return issue

    def edit(self, ref, title=None, desc=None, lane=None, kind=None, note=None):
        issue = self.issue(ref)
        state = self.state_of(issue)
        if not self.actor.human and state != "proposed":
            raise SignalboxError(f"{self.tid(issue['number'])} is approved ({state}); only a person changes approved "
                                 "tasks. Add a note instead, or ask them.")
        if title is not None:
            title = " ".join(title.split())
            if not title:
                raise SignalboxError("Task title is empty")
            self.tracker.update_issue(issue["number"], title=title)
        if desc is not None:
            self.update_meta(self.issue(ref), lambda m: None, desc=desc)
        if lane is not None:
            new = self.lane(lane)
            for lab in issue["labels"]:
                if lab.startswith(LANE_PREFIX):
                    self.tracker.remove_label(issue["number"], lab)
            if new:
                self.tracker.add_labels(issue["number"], [new["label"]])
        if kind is not None:
            if kind not in KINDS:
                raise SignalboxError(f"Unknown kind '{kind}'")
            for lab in issue["labels"]:
                if lab.startswith(KIND_PREFIX):
                    self.tracker.remove_label(issue["number"], lab)
            if kind != "build":
                self.tracker.add_labels(issue["number"], [KIND_PREFIX + kind])
        if note:
            self.note(issue["number"], note)
        self.invalidate()
        return self.issue(ref)

    def depend(self, ref, ons, remove=False):
        issue = self.issue(ref)
        n = issue["number"]
        targets = [number_of(x) for x in ons]
        if remove:
            if not self.actor.human and self.state_of(issue) != "proposed":
                raise SignalboxError(f"Only a person removes dependencies from approved tasks; {self.tid(n)} is "
                                     f"{self.state_of(issue)}")
            self.update_meta(issue, lambda m: m.__setitem__("deps", [d for d in m.get("deps", []) if d not in targets]))
            self._link_deps(n, targets, on=False)
            return targets
        if self.state_of(issue) == "done":
            raise SignalboxError(f"{self.tid(n)} is done; it cannot wait on anything now")
        self._check_deps(n, targets)
        self.update_meta(issue, lambda m: m.__setitem__("deps", sorted(set(m.get("deps", [])) | set(targets))))
        self._link_deps(n, targets)
        return targets

    def withdraw(self, refs, why=""):
        out = []
        for ref in refs:
            issue = self.issue(ref)
            if self.state_of(issue) != "proposed":
                raise SignalboxError(f"{self.tid(issue['number'])} is approved; ask a person to reject it instead")
            self._close_unplanned(issue, why or "Withdrawn by its proposer.")
            out.append(issue)
        return out

    def _close_unplanned(self, issue, why):
        n = issue["number"]
        self.note(n, why, "event")
        self.set_state(issue, "rejected")
        for other in self.issues().values():
            if n in split_body(other["body"])[1].get("deps", []):
                self.update_meta(other, lambda m: m.__setitem__("deps", [d for d in m["deps"] if d != n]))

    # -- claiming and working --------------------------------------------------

    def _record(self, issue, branch=None, worktree=None, pair=None):
        lease = int(self.config["agents"].get("lease_minutes") or 120)
        rec = {"task": self.tid(issue["number"]), "issue": issue["number"], "holder": self.actor.by,
               "label": self.actor.label, "sponsor": self.actor.login, "human": self.actor.human,
               "host": socket.gethostname(), "acquired": iso(now()), "expires": iso(now() + timedelta(minutes=lease))}
        if branch:
            rec["branch"] = branch
        if worktree:
            rec["worktree"] = worktree
        if pair:
            rec["pair"] = login_of(pair)
        return rec

    def my_lock(self, n):
        sha, rec = self.locks.get(n)
        if not sha:
            raise SignalboxError(f"{self.tid(n)} is not locked; claim it first with `{command('claim ' + self.tid(n))}`")
        if rec.get("holder") != self.actor.by:
            raise Locked(f"{self.tid(n)} is held by {rec.get('label')}, not by you ({self.actor.label})", rec, sha)
        return sha, rec

    def claims_of(self, holder):
        return [n for n, (_, rec) in self.lock_table(refresh=True).items() if rec.get("holder") == holder]

    def claim(self, ref, paths=(), pair=None, worktree=True, take_over=False, plan=None):
        """Lock a ready task, move it to doing, and give it a worktree. Returns a dict describing the claim."""
        issue = self.issue(ref)
        n, tid = issue["number"], self.tid(issue["number"])
        board, task = self.task(n)
        kind = self.kind_of(issue)
        vs = K.view_state(task, K.task_map(board))
        if kind == "map":
            raise SignalboxError(f"{tid} is a map, the index of its tickets; claim one of its tickets instead "
                                 f"(`{command('frontier ' + tid)}`)")
        if vs == "blocked":
            raise SignalboxError(f"{tid} is blocked by unfinished {', '.join(K.unmet_deps(task, K.task_map(board)))}")
        if vs == "proposed":
            raise SignalboxError(f"{tid} is a proposal; a person must approve it before anyone starts it")
        if vs not in ("todo", "doing"):
            raise SignalboxError(f"{tid} cannot be claimed: it is {vs}")
        if kind in HITL_KINDS and not self.actor.human and self.config["agents"].get("hitl_needs_pair", True) and not pair:
            raise SignalboxError(f"{tid} is a {kind} ticket: it is resolved with a person, live. Claim it with "
                                 f"--with @their-login once they are in the conversation with you.")
        lane = self.lane_of(issue)
        if lane and lane.get("wip") and vs == "todo":
            doing = sum(1 for t in board["tasks"] if t["state"] == "doing" and (t.get("sb") or {}).get("lane") == lane["name"])
            if doing >= int(lane["wip"]):
                raise SignalboxError(f"Lane {lane['name']} is at its WIP limit ({doing}/{lane['wip']} doing). "
                                     "Finish or pause something there first, or ask its owners to raise the limit.")
        limit = int(self.config["agents"].get("max_claims") or 0)
        if not self.actor.human and limit:
            held = [x for x in self.claims_of(self.actor.by) if x != n]
            if len(held) >= limit:
                raise SignalboxError(f"{self.actor.label} already holds {len(held)} task(s) "
                                     f"({', '.join(self.tid(x) for x in held)}); the limit per agent is {limit}. "
                                     "Finish, pause or hand one off first.")
        declared = list(paths or []) or list(split_body(issue["body"])[1].get("paths", []))
        clashes = self.path_clashes(n, declared)
        if clashes and self.config.get("paths") == "block":
            raise SignalboxError("Declared paths overlap work in progress: " + "; ".join(clashes))
        if clashes:
            self.warnings.append("Declared paths overlap work in progress: " + "; ".join(clashes))

        # The interlock decides between racing claims; everything after it is mirroring.
        rec = self._record(issue, pair=pair)
        cur_sha, cur = self.locks.get(n)
        took_over = None
        if cur_sha:
            if cur.get("holder") == self.actor.by:
                cur = {**cur, "expires": rec["expires"]}
                sha = self.locks.replace(n, cur_sha, cur)
                return {"issue": issue, "sha": sha, "record": cur, "how": "already yours", "worktree": cur.get("worktree"),
                        "branch": cur.get("branch"), "warnings": self.warnings}
            expired = lease_left(cur) <= 0
            if not take_over:
                hint = (f" Its lease ran out {span(lease_left(cur))} ago: take it over with `{command('claim ' + tid + ' --take-over')}`."
                        if expired else " Pick another task, or ask them to hand it off.")
                raise Locked(f"{tid} is locked by {lock_line(cur)}.{hint}", cur, cur_sha)
            if not expired and not self.actor.human:
                raise Locked(f"{tid} is locked by {lock_line(cur)}; only a person can break a live lock", cur, cur_sha)
            if not expired:
                self.require_human("break someone else's live lock")
            took_over = cur
        info = {"issue": issue, "how": "claimed", "warnings": self.warnings}
        if took_over:
            sha = self.locks.replace(n, cur_sha, rec)
        else:
            sha = self.locks.acquire(n, rec)
        # Everything below mirrors the lock. If any step fails, each finished step is undone, newest first, and the
        # lock is given back, so the task is neither stuck in doing nor left with a worktree nobody holds.
        undo = []
        try:
            if plan:
                self.note(n, plan)
            if worktree:
                path, branch, how = self.worktrees.create(tid, issue["title"], self.locks.remote)
                undo.append(lambda: self._drop_worktree(path, branch, how))
                rec.update(branch=branch, worktree=path)
                sha = self.locks.replace(n, sha, rec)
                self.worktrees.lock(path, f"signalbox: {tid} held by {self.actor.label}")
                env = {"SB_TASK": tid, "SB_ISSUE": n, "SB_PORT": self.worktrees.port_for(n), "SB_BRANCH": branch,
                       "SB_HOLDER": self.actor.by, "SB_URL": issue.get("url") or ""}
                self.warnings += self.worktrees.bootstrap(path, env, self.brief(n, rec))
                info.update(worktree=path, branch=branch, worktree_how=how, port=env["SB_PORT"])
            if task["state"] != "doing":
                before = task["state"]
                self.set_state(issue, "doing")
                undo.append(lambda: self.set_state(self.tracker.get_issue(n), before))
            if LOCK_LABEL not in issue["labels"]:
                self.tracker.add_labels(n, [LOCK_LABEL])
                undo.append(lambda: self.tracker.remove_label(n, LOCK_LABEL))
            if self.actor.login and login_of(self.actor.login) not in issue.get("assignees", []):
                self.tracker.set_assignees(n, add=[self.actor.login])
                undo.append(lambda: self.tracker.set_assignees(n, remove=[self.actor.login]))
            if declared and declared != split_body(issue["body"])[1].get("paths", []):
                self.update_meta(self.issue(n), lambda m: m.__setitem__("paths", declared))
            where = f" in `{rec['branch']}`" if rec.get("branch") else ""
            event = f"🔒 Claimed by {self.actor.label}{where} on {rec['host']}, lease until {rec['expires']}."
            if took_over:
                event += f" Took over from {took_over.get('label')}, whose lease " + \
                         ("had run out." if lease_left(took_over) <= 0 else "a person broke.")
            self.note(n, event, "event")
        except Exception:
            for step in reversed(undo):
                try:
                    step()
                except Exception as e:  # keep undoing the rest; doctor reports what is left
                    self.warnings.append(f"Could not undo part of the failed claim of {tid}: {e}")
            try:
                self.locks.release(n, sha)
            except SignalboxError:
                pass
            self.invalidate()
            raise
        self.invalidate()
        info.update(sha=sha, record=rec, took_over=took_over)
        return info

    def _drop_worktree(self, path, branch, how):
        """Undo `Worktrees.create` for a claim that failed: remove a worktree made for it, and the branch too when
        the claim made it. A worktree that was already there stays, unlocked; one with changes is never removed."""
        if not os.path.isdir(path) or how == "existing":
            self.worktrees.unlock(path)
            return
        if self.worktrees.dirty(path) and how != "new":
            self.worktrees.unlock(path)
            return
        self.worktrees.remove(path, force=True)
        if how == "new":
            git("branch", "-D", branch, cwd=self.project.root, check=False)

    def heartbeat(self, ref):
        n = number_of(ref)
        sha, rec = self.my_lock(n)
        lease = int(self.config["agents"].get("lease_minutes") or 120)
        rec = {**rec, "expires": iso(now() + timedelta(minutes=lease)), "beat": iso(now())}
        self.locks.replace(n, sha, rec)
        return rec

    def _release(self, n, sha, rec, keep_assignee=False):
        self.locks.release(n, sha, by=self.actor.by)
        self.tracker.remove_label(n, LOCK_LABEL)
        if not keep_assignee and rec.get("sponsor"):
            try:
                self.tracker.set_assignees(n, remove=[rec["sponsor"]])
            except SignalboxError:
                pass
        if rec.get("worktree") and os.path.isdir(rec["worktree"]):
            self.worktrees.unlock(rec["worktree"])

    def push_branch(self, rec):
        """Push the task's branch so others can see, review and resume it. Returns the remote branch or None."""
        remote, branch, wt = self.locks.remote, rec.get("branch"), rec.get("worktree")
        if not (remote and branch):
            return None
        cwd = wt if wt and os.path.isdir(wt) else self.project.root
        p = git("push", "--quiet", "-u", remote, f"{branch}:{branch}", cwd=cwd, check=False)
        if p.returncode != 0:
            self.warnings.append(f"Could not push {branch} to {remote}: {p.stderr.strip()}")
            return None
        return branch

    def finish(self, ref, summary, kind="summary", pr=False):
        if not (summary or "").strip():
            raise SignalboxError(f"finish needs a summary of what you did, for the person who reviews it, e.g. "
                                 f"`{command('finish ' + str(ref))} -m \"Added X; tests pass\"`")
        issue = self.issue(ref)
        n, tid = issue["number"], self.tid(issue["number"])
        if self.state_of(issue) != "doing":
            raise SignalboxError(f"{tid} is {self.state_of(issue)}; only tasks in progress can be finished")
        sha, rec = self.my_lock(n)
        dirty = self.worktrees.dirty(rec.get("worktree") or "")
        if dirty:
            self.warnings.append(f"The worktree has uncommitted changes ({', '.join(dirty[:5])}); commit them so "
                                 "reviewers and the branch see them.")
        pushed = self.push_branch(rec)
        board = self.board()
        via = K.yolo_cover(board).get(tid.upper())
        to = "done" if via and via.upper() != tid.upper() else "review"
        text = summary.strip()
        if pushed:
            text += f"\n\nBranch: `{pushed}`"
        url = None
        if pr and pushed and self.tracker.kind == "github":
            base = self.worktrees.base(self.locks.remote).split("/", 1)[-1]
            url = self.tracker.create_pull(pushed, base, f"{tid}: {issue['title']}", f"{summary.strip()}\n\nCloses #{n}")
            text += f"\nPull request: {url}"
        self.note(n, text, kind)
        self.set_state(issue, to)
        self._release(n, sha, rec, keep_assignee=True)
        if to == "done":
            self._after_done(self.issue(n))
        return {"to": to, "via": via, "branch": pushed, "pr": url}

    def resolve(self, ref, answer):
        issue = self.issue(ref)
        if self.kind_of(issue) not in DECISION_KINDS:
            raise SignalboxError(f"{self.tid(issue['number'])} is not a decision ticket; use finish")
        return self.finish(ref, f"Resolution: {answer.strip()}", kind="resolution")

    def pause(self, ref, note="", to=None):
        issue = self.issue(ref)
        n, tid = issue["number"], self.tid(issue["number"])
        if self.state_of(issue) != "doing":
            raise SignalboxError(f"{tid} is {self.state_of(issue)}; only tasks in progress can be paused")
        sha, rec = self.my_lock(n)
        pushed = self.push_branch(rec)
        text = (note or "").strip()
        if to:
            text = f"Handed off to @{login_of(to)}. " + text
        if pushed:
            text += f"\n\nWork so far is on `{pushed}`; `{command('claim ' + tid)}` resumes it in a fresh worktree."
        if text.strip():
            self.note(n, text.strip(), "event" if not note else "note")
        self.set_state(issue, "todo")
        self._release(n, sha, rec)
        if to:
            self.tracker.set_assignees(n, add=[login_of(to)])
        return {"branch": pushed}

    def ask(self, ref, question):
        issue = self.issue(ref)
        if self.state_of(issue) != "doing":
            raise SignalboxError(f"{self.tid(issue['number'])} is {self.state_of(issue)}; ask on a task you work on")
        self.note(issue["number"], f"{K.QUESTION} {question.strip()}", "question")

    def add_note(self, ref, text):
        issue = self.issue(ref)
        if self.state_of(issue) == "done":
            raise SignalboxError(f"{self.tid(issue['number'])} is done; propose a follow-up task instead")
        self.note(issue["number"], text)

    # -- human decisions -------------------------------------------------------

    def approve(self, refs):
        out = []
        for ref in refs:
            issue = self.issue(ref)
            self.require_approver(issue, "approve")
            if self.state_of(issue) != "proposed":
                raise SignalboxError(f"{self.tid(issue['number'])} is {self.state_of(issue)}, not a proposal")
            self.set_state(issue, "todo")
            out.append(issue)
        return out

    def request_changes(self, ref, text):
        issue = self.issue(ref)
        self.require_human("ask for changes")
        if self.state_of(issue) != "proposed":
            raise SignalboxError(f"{self.tid(issue['number'])} is not a proposal")
        self.note(issue["number"], f"{K.CHANGES} {text.strip()}", "changes")

    def accept(self, refs):
        out = []
        for ref in refs:
            issue = self.issue(ref)
            self.require_approver(issue, "mark done")
            if self.state_of(issue) != "review":
                raise SignalboxError(f"{self.tid(issue['number'])} is {self.state_of(issue)}; only work in review can be accepted")
            self.set_state(issue, "done")
            self._after_done(self.issue(issue["number"]))
            out.append(issue)
        return out

    def send_back(self, ref, feedback):
        issue = self.issue(ref)
        self.require_approver(issue, "send back")
        if self.state_of(issue) != "review":
            raise SignalboxError(f"{self.tid(issue['number'])} is not in review")
        if not (feedback or "").strip():
            raise SignalboxError("Say what to change: the feedback becomes a note the next holder reads first")
        self.note(issue["number"], f"Sent back: {feedback.strip()}", "feedback")
        self.set_state(issue, "todo")

    def reject(self, refs, why=""):
        for ref in refs:
            issue = self.issue(ref)
            self.require_approver(issue, "reject")
            self._close_unplanned(issue, f"Rejected by {self.actor.label}. {why}".strip())

    def set_yolo(self, ref, on=True):
        issue = self.issue(ref)
        self.require_approver(issue, "turn YOLO " + ("on" if on else "off"))
        n = issue["number"]
        if on:
            self.tracker.add_labels(n, [YOLO_LABEL])
            board = self.board()
            for key in K._upstream(K.task_map(board), K.task_map(board)[self.tid(n).upper()]) | {self.tid(n).upper()}:
                t = K.task_map(board).get(key)
                if t and t["state"] == "proposed":
                    self.set_state(self.issue(t["sb"]["issue"]), "todo")
        else:
            self.tracker.remove_label(n, YOLO_LABEL)
        self.invalidate()

    def unlock(self, ref, why=""):
        """Release a lock: your own, or anyone's when you are a person at the terminal."""
        n = number_of(ref)
        sha, rec = self.locks.get(n)
        if not sha:
            raise SignalboxError(f"{self.tid(n)} is not locked")
        if rec.get("holder") != self.actor.by:
            self.require_human("break someone else's lock")
        issue = self.issue(n)
        self._release(n, sha, rec)
        if self.state_of(issue) == "doing":
            self.set_state(issue, "todo")
        self.note(n, f"🔓 Lock of {rec.get('label')} released by {self.actor.label}. {why}".strip(), "event")
        return rec

    def _after_done(self, issue):
        """A decision ticket that is done adds its answer to its map's Decisions so far, as Wayfinder does."""
        _, meta = split_body(issue["body"])
        parent = meta.get("map")
        if not parent or self.kind_of(issue) not in DECISION_KINDS:
            return
        answer = ""
        for c in reversed(self.tracker.list_comments(issue["number"])):
            text = parse_note(c)["text"]
            if text.startswith("Resolution:"):
                answer = text[len("Resolution:"):].strip()
                break
        gist = K._gist(answer) if answer else "resolved"
        try:
            m = self.issue(parent)
        except SignalboxError:
            return
        desc, _ = split_body(m["body"])
        current = section(desc, "Decisions so far")
        line = f"- [{issue['title']}]({issue.get('url') or '#' + str(issue['number'])}): {gist}"
        if line not in current:
            self.update_meta(m, lambda _m: None, desc=set_section(desc, "Decisions so far", (current + "\n" + line).strip()))

    # -- maps (Wayfinder) ------------------------------------------------------

    def map_new(self, title, destination, notes="", lane=None):
        if not destination.strip():
            raise SignalboxError("A map starts from its destination: what reaching the end of it looks like")
        return self.propose(title, map_template(destination, notes), lane=lane, kind="map")

    def map_issue(self, ref):
        issue = self.issue(ref)
        if self.kind_of(issue) != "map":
            raise SignalboxError(f"{self.tid(issue['number'])} is not a map")
        return issue

    def fog(self, ref, add=None, remove=None):
        issue = self.map_issue(ref)
        desc, _ = split_body(issue["body"])
        items = bullets(section(desc, "Not yet specified"))
        if add:
            items.append(" ".join(add.split()))
        if remove:
            keep = [x for x in items if remove.lower() not in x.lower()]
            if len(keep) == len(items):
                raise SignalboxError(f"No fog on {self.tid(issue['number'])} matches '{remove}'")
            items = keep
        if add or remove:
            self.update_meta(issue, lambda m: None, desc=set_section(desc, "Not yet specified", "\n".join(f"- {x}" for x in items)))
        return items

    def scope_out(self, ref, why):
        issue = self.issue(ref)
        parent = split_body(issue["body"])[1].get("map")
        if not parent:
            raise SignalboxError(f"{self.tid(issue['number'])} belongs to no map")
        if self.actor.human:
            self.require_approver(issue, "rule out of scope")
        elif self.state_of(issue) != "proposed":
            raise SignalboxError("Only a person rules approved tickets out of scope; propose it to them")
        self._close_unplanned(issue, f"Out of scope: {why}")
        m = self.issue(parent)
        desc, _ = split_body(m["body"])
        line = f"- [{issue['title']}]({issue.get('url') or '#' + str(issue['number'])}): {why.strip()}"
        self.update_meta(m, lambda _m: None, desc=set_section(desc, "Out of scope", (section(desc, "Out of scope") + "\n" + line).strip()))

    def frontier(self, map_ref=None):
        board = self.board()
        tasks = K.task_map(board)
        parent = number_of(map_ref) if map_ref else None
        out = []
        for t in board["tasks"]:
            sb = t["sb"]
            if sb["kind"] == "map" or (parent and sb.get("map") != parent):
                continue
            if K.view_state(t, tasks) == "todo" and not sb.get("lock"):
                out.append(t)
        return board, out

    # -- the brief a worktree carries ----------------------------------------

    def brief(self, n, rec):
        board, t = self.task(n)
        tasks = K.task_map(board)
        sb = t["sb"]
        lines = [f"# {t['id']}: {t['title']}", "",
                 f"- Issue: {sb.get('url') or '#' + str(n)}",
                 f"- Kind: {sb['kind']}" + (" (HITL: resolve it with the person, live; never answer for them)" if sb.get("hitl") else ""),
                 f"- Held by: {rec['label']} on {rec['host']}, lease until {rec['expires']}",
                 f"- Branch: {rec.get('branch', '-')}"]
        if sb.get("lane"):
            lines.append(f"- Lane: {sb['lane']}")
        if sb.get("paths"):
            lines.append(f"- Declared paths: {', '.join(sb['paths'])}")
        if t["deps"]:
            lines.append("- Depends on: " + ", ".join(f"{d} ({tasks[d.upper()]['state']})" for d in t["deps"] if d.upper() in tasks))
        lines += ["", "## Description", "", t["desc"] or "_No description._"]
        if sb.get("map"):
            m = tasks.get(self.tid(sb["map"]).upper())
            if m:
                lines += ["", f"## Map: {m['title']} ({m['id']})", "", "### Destination", "", section(m["desc"], "Destination") or "-",
                          "", "### Decisions so far", "", section(m["desc"], "Decisions so far") or "_None yet._"]
        if t["notes"]:
            lines += ["", "## Notes", ""]
            for note in t["notes"][-12:]:
                lines.append(f"- **{note['by']}** ({note['at']}): " + note["text"].replace("\n", "\n  "))
        lines += ["", "## Commands", "",
                  f"- Keep the lease alive on long work: `{command('heartbeat ' + t['id'])}`",
                  f"- Record findings: `{command('note ' + t['id'])} \"...\"`",
                  f"- Ask the person and wait: `{command('ask ' + t['id'])} \"...\"`",
                  f"- Hand over for review: `{command('finish ' + t['id'])} -m \"what changed, how you checked it, what to look at\"`",
                  f"- Stop and let someone else resume: `{command('pause ' + t['id'])} -m \"where I left off\"`", ""]
        return "\n".join(lines)

    # -- radar: overlapping work ---------------------------------------------

    def path_clashes(self, n, declared):
        out = []
        if not declared:
            return out
        board = self.board()
        for t in board["tasks"]:
            sb = t["sb"]
            if sb["issue"] == n or not sb.get("lock"):
                continue
            hits = sorted({f"{a} ~ {b}" for a in declared for b in sb.get("paths", []) if paths_overlap(a, b)})
            if hits:
                out.append(f"{t['id']} ({sb['lock'].get('label')}): {', '.join(hits)}")
        return out

    def radar(self):
        """Pairs of locked tasks whose branches or worktrees touch the same files, or whose declared paths overlap."""
        board = self.board()
        active = [t for t in board["tasks"] if t["sb"].get("lock")]
        base = self.worktrees.base(self.locks.remote)
        files = {}
        for t in active:
            lock = t["sb"]["lock"]
            files[t["id"]] = changed_files(self.project, lock.get("branch"), base, self.locks.remote, lock.get("worktree")) \
                if lock.get("branch") else set()
        out = []
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                same = sorted(files[a["id"]] & files[b["id"]])
                declared = sorted({f"{x} ~ {y}" for x in a["sb"].get("paths", []) for y in b["sb"].get("paths", []) if paths_overlap(x, y)})
                touch = sorted(f for f in files[a["id"]] if any(fnmatch.fnmatch(f, p) for p in b["sb"].get("paths", []))) + \
                    sorted(f for f in files[b["id"]] if any(fnmatch.fnmatch(f, p) for p in a["sb"].get("paths", [])))
                if same or declared or touch:
                    out.append({"a": a, "b": b, "files": same, "declared": declared, "touch": sorted(set(touch) - set(same))})
        return out, files

    # -- the browser board: a saved board becomes moves on the hub -------------

    def apply_board(self, old, new):
        """Turn the difference between two boards (the mirror and what the browser saved) into hub operations, as the
        person using the browser. Returns the list of things done."""
        done = []
        old_t = {t["id"].upper(): t for t in old["tasks"]}
        new_t = {t["id"].upper(): t for t in new["tasks"]}
        old_g = {g["id"]: g for g in old["groups"]}
        new_g = {g["id"]: g for g in new["groups"]}
        lanes = {lane["id"]: lane for lane in self.lanes()}

        for gid, g in new_g.items():
            if gid not in old_g and not gid.startswith("M"):
                label = LANE_PREFIX + g["name"]
                self.tracker.ensure_label(label, "14b8a6", lane_description({"id": gid, "owners": [], "wip": None}))
                lanes[gid] = {"id": gid, "name": g["name"], "label": label, "owners": [], "wip": None, "configured": False}
                done.append(f"lane {g['name']}")
            elif gid in old_g and g["name"] != old_g[gid]["name"] and gid in lanes and not lanes[gid]["configured"]:
                self.tracker.update_label(lanes[gid]["label"], new_name=LANE_PREFIX + g["name"])
                lanes[gid] = {**lanes[gid], "name": g["name"], "label": LANE_PREFIX + g["name"]}
                done.append(f"renamed lane {g['name']}")
        for gid in old_g:
            if gid not in new_g and gid in lanes and not lanes[gid]["configured"]:
                self.tracker.delete_label(lanes[gid]["label"])
                done.append(f"removed lane {old_g[gid]['name']}")
        self._labels = None

        def lane_label(group):
            lane = lanes.get(group)
            return lane["label"] if lane else None

        # New tasks first, so dependencies saved with them can point at their issues.
        ids = {}
        for k, t in new_t.items():
            if k in old_t:
                ids[k] = (old_t[k].get("sb") or {}).get("issue") or number_of(t["id"])
                continue
            group = t.get("group")
            parent = int(group[1:]) if group and group.startswith("M") and group[1:].isdigit() else None
            issue = self.propose(t["title"], t.get("desc", ""), lane=lanes.get(group), map_ref=parent,
                                 state=t["state"] if t["state"] in ("proposed", "todo", "review") else "todo")
            ids[k] = issue["number"]
            for note in t.get("notes") or []:
                self.note(issue["number"], note["text"])
            if t["state"] == "done":
                self.set_state(self.issue(issue["number"]), "done")
            done.append(f"created {self.tid(issue['number'])}")

        for k, t in old_t.items():
            if k not in new_t:
                issue = self.issue(t["sb"]["issue"])
                if self.state_of(issue) != "proposed":
                    self.require_approver(issue, "delete")
                self._close_unplanned(issue, f"Deleted on the board by {self.actor.label}.")
                done.append(f"deleted {t['id']}")

        for k, t in new_t.items():
            o = old_t.get(k)
            n = ids[k]
            if o is None:
                deps = sorted({ids[d.upper()] for d in t.get("deps", []) if d.upper() in ids})
                if deps:
                    self.update_meta(self.issue(n), lambda m, deps=deps: m.__setitem__("deps", deps))
                    self._link_deps(n, deps)
                if t.get("yolo"):
                    self.tracker.add_labels(n, [YOLO_LABEL])
                continue
            same_notes = [(x.get("cid"), x.get("text")) for x in t.get("notes", [])] == \
                [(x.get("cid"), x.get("text")) for x in o.get("notes", [])]
            if same_notes and all(t.get(f) == o.get(f) for f in ("title", "desc", "deps", "group", "yolo", "state")):
                continue  # untouched: no need to ask the hub about it
            issue = self.issue(n)
            if t["title"] != o["title"]:
                self.tracker.update_issue(n, title=t["title"])
                done.append(f"retitled {t['id']}")
            new_deps = sorted({ids.get(d.upper()) or number_of(d) for d in t.get("deps", [])})
            old_deps = sorted({number_of(d) for d in o.get("deps", [])})
            if t.get("desc", "") != o.get("desc", "") or new_deps != old_deps:
                self.update_meta(self.issue(n), lambda m, d=new_deps: m.__setitem__("deps", d), desc=t.get("desc", ""))
                self._link_deps(n, sorted(set(new_deps) - set(old_deps)))
                self._link_deps(n, sorted(set(old_deps) - set(new_deps)), on=False)
                done.append(f"edited {t['id']}")
            if t.get("group") != o.get("group"):
                for lab in issue["labels"]:
                    if lab.startswith(LANE_PREFIX):
                        self.tracker.remove_label(n, lab)
                if lane_label(t.get("group")):
                    self.tracker.add_labels(n, [lane_label(t.get("group"))])
                done.append(f"moved {t['id']}")
            if bool(t.get("yolo")) != bool(o.get("yolo")):
                self.require_approver(issue, "change YOLO")
                (self.tracker.add_labels(n, [YOLO_LABEL]) if t.get("yolo") else self.tracker.remove_label(n, YOLO_LABEL))
                done.append(f"YOLO {'on' if t.get('yolo') else 'off'} {t['id']}")
            old_notes = {x.get("cid"): x for x in o.get("notes", []) if x.get("cid")}
            kept = set()
            for note in t.get("notes", []):
                cid = note.get("cid")
                if cid and cid in old_notes:
                    kept.add(cid)
                    if note["text"] != old_notes[cid]["text"]:
                        self._rewrite_comment(n, cid, note["text"])
                elif not cid:
                    self.note(n, note["text"], "feedback" if note["text"].startswith("Sent back:") else
                              "changes" if note["text"].startswith(K.CHANGES) else "note")
                    done.append(f"noted {t['id']}")
            for cid in set(old_notes) - kept:
                self.tracker.delete_comment(n, cid)
            if t["state"] != o["state"]:
                self._browser_move(self.issue(n), o["state"], t["state"])
                done.append(f"{t['id']} {o['state']} -> {t['state']}")
        self.invalidate()
        return done

    def _rewrite_comment(self, n, cid, text):
        """Edit a note's text and keep its signature, so an edited agent note still shows the agent."""
        body = next((c["body"] for c in self.tracker.list_comments(n) if c["id"] == cid), None)
        if body is None:
            return
        m = NOTE_RE.match(body.replace("\r\n", "\n"))
        if m:
            rest = body.replace("\r\n", "\n")[m.end():]
            sig = rest.split("\n", 1)[0] if rest.startswith("**") else ""
            body = f"{m.group(0).rstrip()}\n{sig}\n\n{text.strip()}" if sig else f"{m.group(0).rstrip()}\n{text.strip()}"
        else:
            body = text.strip()
        self.tracker.edit_comment(n, cid, body)
        self._comments.pop(n, None)

    def _browser_move(self, issue, before, after):
        """A state change made in the browser, checked against the same rules as the CLI."""
        n = issue["number"]
        if after == "doing":
            raise SignalboxError(f"Start {self.tid(n)} by claiming it, so it gets its lock and worktree: "
                                 f"`{command('claim ' + self.tid(n))}`")
        if before == "proposed" and after == "todo":
            return self.approve([n])
        if before == "review" and after == "done":
            return self.accept([n])
        self.require_approver(issue, f"move a task from {before} to {after}")
        if before == "doing":
            sha, rec = self.locks.get(n)
            if sha:
                self._release(n, sha, rec)
                self.note(n, f"🔓 {self.actor.label} moved this out of doing; the lock of {rec.get('label')} is released.", "event")
        self.set_state(issue, after)
        if after == "done":
            self._after_done(self.issue(n))

    # -- mirror: the board file the browser and `kanvas` read ------------------

    def mirror_path(self):
        return self.project.state_path(MIRROR_FILE)

    def write_mirror(self):
        """Rebuild the mirror from the hub, keeping the browser's own layout keys and task order."""
        board = self.board()
        path = self.mirror_path()
        try:
            prev = K.parse_board(K.read_text_file(path))
        except (OSError, K.KanvasError):
            prev = None
        if prev:
            for k, v in prev.items():
                if k not in board:
                    board[k] = v
            order = {t["id"].upper(): i for i, t in enumerate(prev["tasks"])}
            board["tasks"].sort(key=lambda t: (order.get(t["id"].upper(), len(order)), t["sb"]["issue"]))
            gorder = {g["id"]: i for i, g in enumerate(prev["groups"])}
            board["groups"].sort(key=lambda g: gorder.get(g["id"], len(gorder)))
        text = K.dumps(board)
        try:
            current = K.read_text_file(path)
        except OSError:
            current = None
        if text != current:
            K.write_text_atomic(path, text)
        return board


def lane_description(lane):
    parts = [f"Signalbox lane [{lane['id']}]"]
    if lane.get("owners"):
        parts.append("owners: " + ", ".join(lane["owners"]))
    if lane.get("wip"):
        parts.append(f"wip {lane['wip']}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------


def task_flags(t, tasks, cover):
    sb = t.get("sb") or {}
    out = ""
    if sb.get("kind") and sb["kind"] != "build":
        out += f"  [{sb['kind']}{', HITL' if sb.get('hitl') else ''}]"
    if sb.get("lock"):
        lock = sb["lock"]
        out += f"  🔒 {lock.get('label')}" + (" (lease expired)" if lock.get("expired") else "")
    out += K.yolo_mark(t, cover) + K.waiting_mark(t) + K.changes_mark(t, cover)
    return out


def print_task(board, t):
    tasks = K.task_map(board)
    cover = K.yolo_cover(board)
    sb = t["sb"]
    print(f"{t['id']}  {t['title']}")
    print(f"  State:  {K.view_state(t, tasks)}" + (f"  (YOLO through {cover[t['id'].upper()]})" if t["id"].upper() in cover else ""))
    print(f"  Kind:   {sb['kind']}" + ("  (HITL: worked live with a person)" if sb.get("hitl") else ""))
    if sb.get("lane"):
        print(f"  Lane:   {sb['lane']}")
    if sb.get("map"):
        m = tasks.get(f"{board_prefix(board)}-{sb['map']}".upper())
        print(f"  Map:    {m['id']} {m['title']}" if m else f"  Map:    #{sb['map']}")
    print(f"  Issue:  {sb.get('url') or '#' + str(sb['issue'])}")
    if sb.get("lock"):
        print(f"  Lock:   {lock_line(sb['lock'])}")
        if sb["lock"].get("worktree"):
            print(f"  Worktree: {sb['lock']['worktree']}")
    if sb.get("assignees"):
        print(f"  Assigned: {', '.join('@' + a for a in sb['assignees'])}")
    if sb.get("paths"):
        print(f"  Paths:  {', '.join(sb['paths'])}")
    if t.get("by"):
        print(f"  Created by {t['by']}" + (f" at {t['at']}" if t.get("at") else ""))
    for dep in t["deps"]:
        d = tasks.get(dep.upper())
        print(f"  Depends on {dep}: " + (f"{d['title']} ({K.view_state(d, tasks)})" if d else "missing"))
    waiting = [x for x in board["tasks"] if t["id"].upper() in {d.upper() for d in x["deps"]}]
    for w in waiting:
        print(f"  Blocks {w['id']}: {w['title']} ({K.view_state(w, tasks)})")
    if t["desc"]:
        print("\n" + K._indent(t["desc"]))
    if sb["kind"] == "map":
        fog = bullets(section(t["desc"], "Not yet specified"))
        frontier = [x["id"] for x in board["tasks"] if (x.get("sb") or {}).get("map") == sb["issue"]
                    and K.view_state(x, tasks) == "todo" and not x["sb"].get("lock")]
        print(f"\n  Frontier: {', '.join(frontier) or 'nothing takeable now'} · fog: {len(fog)} patch(es)")
    if t["notes"]:
        print("\nNotes")
        for note in t["notes"]:
            print(f"  [{note['at']}] {note['by']}:")
            print(K._indent(note["text"], "    "))


def board_prefix(board):
    t = board["tasks"][0] if board["tasks"] else None
    return t["id"].rsplit("-", 1)[0] if t else "SB"


def print_rows(rows, tasks, cover, indent="  "):
    w = K.id_width(rows)
    for t in rows:
        print(f"{indent}{t['id']:<{w}}  {t['title']}{task_flags(t, tasks, cover)}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def open_hub(args, human=None):
    project = Project(getattr(args, "dir", None) if getattr(args, "command", "") == "init" else None)
    project.require_config()
    tracker = make_tracker(project, project.config)
    actor = resolve_actor(args, tracker, human)
    return Hub(project, tracker, actor, interactive=person_present())


def text_arg(args, name="message", what="text"):
    return K.read_input(getattr(args, name, None), getattr(args, "stdin", False), getattr(args, "file", None), what)


def warn(hub):
    for w in dict.fromkeys(hub.warnings):
        print(f"Warning: {w}", file=sys.stderr)


def cmd_status(args):
    hub = open_hub(args)
    board = hub.write_mirror()
    tasks = K.task_map(board)
    cover = K.yolo_cover(board)
    buckets = {k: [] for k in ("doing", "review", "todo", "blocked", "proposed", "done")}
    for t in board["tasks"]:
        if t["sb"]["kind"] != "map":
            buckets.setdefault(K.view_state(t, tasks), []).append(t)
    print(f"{board['title']} · hub {hub.tracker.describe()} · locks on {hub.locks.where()} · you are {hub.actor.label}")
    total = sum(len(v) for v in buckets.values())
    if not board["tasks"]:
        print(f"No tasks yet. Propose some with `{command('propose')}` or `{command('batch')}`, or chart a map with "
              f"`{command('map new')}`.")
    else:
        names = {"todo": "ready"}
        print(f"{total} tasks: " + ", ".join(f"{len(v)} {names.get(k, k)}" for k, v in buckets.items() if v))
    lanes = hub.lanes()
    if lanes:
        parts = []
        for lane in lanes:
            members = [t for t in board["tasks"] if t["sb"].get("lane") == lane["name"]]
            doing = sum(1 for t in members if t["state"] == "doing")
            wip = f", wip {doing}/{lane['wip']}" if lane.get("wip") else ""
            owners = f", owners {', '.join(lane['owners'])}" if lane["owners"] else ""
            parts.append(f"{lane['name']} [{lane['id']}] {sum(1 for t in members if t['state'] == 'done')}/{len(members)}{wip}{owners}")
        print("Lanes: " + "; ".join(parts))
    maps = [t for t in board["tasks"] if t["sb"]["kind"] == "map" and t["state"] != "done"]
    for m in maps:
        front = [t["id"] for t in board["tasks"] if t["sb"].get("map") == m["sb"]["issue"] and K.view_state(t, tasks) == "todo"
                 and not t["sb"].get("lock")]
        dest = K._gist(m["sb"].get("destination") or "")
        print(f"Map {m['id']} {m['title']}: destination \"{dest}\" · frontier {', '.join(front) or 'empty'} · fog {m['sb'].get('fog', 0)}")
    locked = [t for t in board["tasks"] if t["sb"].get("lock")]
    if locked:
        print(f"\nInterlocks ({len(locked)})")
        w = K.id_width(locked)
        for t in locked:
            print(f"  {t['id']:<{w}}  {lock_line(t['sb']['lock'])}")
    sections_ = (("doing", "Doing"), ("review", "Waiting for human review"), ("todo", "Ready to claim"),
                 ("blocked", "Blocked"), ("proposed", "Proposed, awaiting human approval"))
    for key, label in sections_:
        rows = [t for t in buckets[key] if t["sb"]["kind"] != "map"]
        if not rows:
            continue
        print(f"\n{label} ({len(rows)})")
        w = K.id_width(rows)
        for t in rows:
            line = f"  {t['id']:<{w}}  {t['title']}"
            if key == "blocked":
                line += f"  (waiting on {', '.join(K.unmet_deps(t, tasks))})"
            print(line + task_flags(t, tasks, cover))
    errors, warnings = K.problems(board)
    stale = [t["id"] for t in board["tasks"] if t["state"] == "doing" and not t["sb"].get("lock")]
    orphan = [t["id"] for t in board["tasks"] if t["sb"].get("lock") and t["state"] != "doing"]
    if stale:
        warnings.append(f"Doing without an interlock: {', '.join(stale)} (claimed outside Signalbox, or a lock was lost); "
                        f"claim it again or run `{command('doctor')}`")
    if orphan:
        warnings.append(f"Locked but not in progress: {', '.join(orphan)}; `{command('gc')}` releases them")
    if errors or warnings:
        print("\nProblems")
        for e in errors:
            print(f"  error: {e}")
        for w in warnings:
            print(f"  warning: {w}")
    warn(hub)


def cmd_list(args):
    hub = open_hub(args)
    board = hub.board()
    tasks = K.task_map(board)
    cover = K.yolo_cover(board)
    rows = board["tasks"]
    flt = (args.filter or "").strip().lower()
    if flt:
        state = "todo" if flt == "ready" else flt
        if state in K.STATES or state == "blocked":
            rows = [t for t in rows if K.view_state(t, tasks) == state]
        elif flt in KINDS:
            rows = [t for t in rows if t["sb"]["kind"] == flt]
        else:
            g = K.find_group(board, args.filter)
            rows = [t for t in rows if t.get("group") == g["id"]]
    if not rows:
        print("No matching tasks.")
        return
    for g in board["groups"] + [{"id": None, "name": "No lane"}]:
        members = [t for t in rows if t.get("group") == g["id"]]
        if members:
            print(f"{g['name']}" + (f" [{g['id']}]" if g["id"] else ""))
            print_rows(members, tasks, cover)


def cmd_show(args):
    hub = open_hub(args)
    board, t = hub.task(args.id)
    print_task(board, t)


def cmd_ready(args):
    hub = open_hub(args)
    board = hub.board()
    tasks = K.task_map(board)
    rows = [t for t in board["tasks"] if K.view_state(t, tasks) == "todo" and not t["sb"].get("lock") and t["sb"]["kind"] != "map"]
    if rows:
        print_rows(rows, tasks, K.yolo_cover(board), indent="")
    else:
        print("Nothing is ready to claim.")


def cmd_frontier(args):
    hub = open_hub(args)
    board, rows = hub.frontier(args.map)
    tasks = K.task_map(board)
    if args.map:
        _, m = hub.task(args.map, board)
        print(f"{m['id']} {m['title']}\nDestination: {m['sb'].get('destination') or '-'}\n")
    if rows:
        print_rows(rows, tasks, K.yolo_cover(board), indent="")
    else:
        print("The frontier is empty: everything open is blocked, claimed, or waiting for a person.")


def cmd_blocked(args):
    hub = open_hub(args)
    board = hub.board()
    tasks = K.task_map(board)
    rows = [t for t in board["tasks"] if K.view_state(t, tasks) == "blocked"]
    if not rows:
        print("No blocked tasks.")
    for t in rows:
        print(f"{t['id']}  {t['title']}")
        for dep in K.unmet_deps(t, tasks):
            d = tasks.get(dep.upper())
            print(f"  waiting on {dep}  " + (f"{d['title']}  ({K.view_state(d, tasks)})" if d else "(missing)"))


def cmd_check(args):
    hub = open_hub(args)
    board = hub.board()
    errors, warnings = K.problems(board)
    for e in errors:
        print(f"error: {e}")
    for w in warnings:
        print(f"warning: {w}")
    if not errors:
        print(f"OK: {len(board['tasks'])} tasks in {len(board['groups'])} lanes and maps")
    return 1 if errors else 0


def cmd_whoami(args):
    hub = open_hub(args)
    a = hub.actor
    print(f"{a.label}: {'a person' if a.human else 'an agent'}" + ("" if a.human else f" acting for @{a.sponsor}"))
    print(f"Hub: {hub.tracker.describe()} · locks on {hub.locks.where()}")
    if a.human and not hub.interactive:
        print("No person at this terminal: human decisions (approve, accept, send back) are refused here.")


def cmd_claim(args):
    hub = open_hub(args)
    plan = text_arg(args, "message", "plan")
    info = hub.claim(args.id, paths=args.paths or (), pair=args.pair, worktree=not args.no_worktree,
                     take_over=args.take_over, plan=plan or None)
    tid = hub.tid(info["issue"]["number"])
    if info["how"] == "already yours":
        print(f"You already hold {tid}; the lease is renewed.")
    else:
        took = f", taking over from {info['took_over'].get('label')}" if info.get("took_over") else ""
        print(f"Claimed {tid}: {info['issue']['title']} (locked on {hub.locks.where()}{took})")
    if info.get("worktree"):
        how = {"new": "new branch", "resumed": "resumed from the existing branch", "existing": "existing worktree"}
        print(f"Worktree: {info['worktree']}  ({info.get('branch')}, {how.get(info.get('worktree_how'), '')})")
        if info.get("port"):
            print(f"Port: {info['port']} (SB_PORT in {STATE_DIR}/env)")
        print(f"Brief: {os.path.join(info['worktree'], STATE_DIR, 'TASK.md')}")
        print(f"Work there: cd {shlex.quote(info['worktree'])}")
    print(f"The lease lasts {hub.config['agents']['lease_minutes']} minutes; keep it with `{command('heartbeat ' + tid)}`.")
    warn(hub)


def cmd_heartbeat(args):
    hub = open_hub(args)
    rec = hub.heartbeat(args.id)
    print(f"{rec['task']}: lease renewed until {rec['expires']}")


def cmd_finish(args):
    hub = open_hub(args)
    res = hub.finish(args.id, text_arg(args, "message", "summary"), pr=args.pr)
    tid = hub.tid(number_of(args.id))
    if res["to"] == "done":
        print(f"Finished {tid} (doing -> done): YOLO on {res['via']} covers it, so it skips review. The lock is released.")
    else:
        print(f"Finished {tid} (doing -> review). The lock is released; a person reviews it next.")
    if res.get("branch"):
        print(f"Branch pushed: {res['branch']}")
    if res.get("pr"):
        print(f"Pull request: {res['pr']}")
    warn(hub)


def cmd_resolve(args):
    hub = open_hub(args)
    res = hub.resolve(args.id, text_arg(args, "message", "answer"))
    print(f"Resolved {hub.tid(number_of(args.id))} ({'done' if res['to'] == 'done' else 'in review'}): "
          "once it is accepted, its answer joins the map's Decisions so far.")
    warn(hub)


def cmd_pause(args):
    hub = open_hub(args)
    res = hub.pause(args.id, text_arg(args, "message", "note"))
    print(f"Paused {hub.tid(number_of(args.id))} (doing -> todo); the lock is released and anyone can claim it.")
    if res.get("branch"):
        print(f"Work so far is pushed on {res['branch']}.")
    warn(hub)


def cmd_handoff(args):
    hub = open_hub(args)
    res = hub.pause(args.id, text_arg(args, "message", "note"), to=args.to)
    print(f"Handed {hub.tid(number_of(args.id))} to @{login_of(args.to)}: the lock is released and the task assigned to them.")
    if res.get("branch"):
        print(f"They resume it with `{command('claim ' + hub.tid(number_of(args.id)))}`, which checks out {res['branch']}.")
    warn(hub)


def cmd_note(args):
    hub = open_hub(args)
    text = text_arg(args, "text", "note")
    if not text:
        raise SignalboxError("Note text is empty")
    hub.add_note(args.id, text)
    print(f"Added a note to {hub.tid(number_of(args.id))}")


def cmd_ask(args):
    hub = open_hub(args)
    text = text_arg(args, "text", "question")
    if not text:
        raise SignalboxError("The question is empty")
    hub.ask(args.id, text)
    tid = hub.tid(number_of(args.id))
    print(f"{tid} now shows that you wait for a person's reply. Ask the same question in the conversation and stop; "
          f"record the answer with `{command('note ' + tid)}`.")


def cmd_propose(args):
    hub = open_hub(args)
    desc = text_arg(args, "desc", "description")
    issue = hub.propose(args.title, desc, lane=args.lane, kind=args.kind, deps=args.deps or (), map_ref=args.map,
                        paths=args.paths or ())
    tid = hub.tid(issue["number"])
    state = hub.state_of(issue)
    print(f"{'Proposed' if state == 'proposed' else 'Added'} {tid}: {issue['title']}"
          + (" — a person approves it before work starts." if state == "proposed" else ""))
    print(f"Issue: {issue.get('url')}")


def cmd_batch(args):
    hub = open_hub(args)
    text = K.read_text_file(args.file) if args.file else sys.stdin.buffer.read().decode("utf-8-sig")
    try:
        spec = json.loads(text)
    except ValueError as e:
        raise SignalboxError(f"Batch is not valid JSON: {e}")
    if not isinstance(spec, dict) or not isinstance(spec.get("tasks", []), list):
        raise SignalboxError('Batch must be {"lanes": [...], "tasks": [{"title": ..., "lane": ..., "deps": [...]}]}')
    # Check everything before creating anything: issues cannot be taken back as cleanly as a file write.
    titles = set()
    known_lanes = {x["name"].lower() for x in hub.lanes()} | {x["id"].lower() for x in hub.lanes()}
    new_lanes = [" ".join(str(x).split()) for x in spec.get("lanes", [])]
    for i, t in enumerate(spec.get("tasks", [])):
        if not isinstance(t, dict) or not str(t.get("title", "")).strip():
            raise SignalboxError(f"Task {i + 1} in the batch has no title")
        if t.get("kind", "build") not in KINDS:
            raise SignalboxError(f"Task '{t['title']}': unknown kind '{t['kind']}'")
        lane = t.get("lane") or t.get("group")
        if lane and lane.lower() not in known_lanes and lane not in new_lanes:
            raise SignalboxError(f"Task '{t['title']}': lane '{lane}' does not exist; list it under \"lanes\"")
        for d in t.get("deps", []):
            if d not in titles:
                number_of(d)
        titles.add(" ".join(str(t["title"]).split()))
    for name in new_lanes:
        if name.lower() not in known_lanes:
            add_lane(hub, name)
    hub._labels = None
    made = {}
    for t in spec.get("tasks", []):
        deps = [made[d] if d in made else d for d in t.get("deps", [])]
        issue = hub.propose(t["title"], t.get("desc", ""), lane=t.get("lane") or t.get("group"), kind=t.get("kind", "build"),
                            deps=deps, map_ref=t.get("map") or spec.get("map"), paths=t.get("paths") or ())
        made[" ".join(str(t["title"]).split())] = hub.tid(issue["number"])
        print(f"{'Proposed' if hub.state_of(issue) == 'proposed' else 'Added'} {hub.tid(issue['number'])}: {issue['title']}")
    print(f"{len(made)} task(s) on {hub.tracker.describe()}.")


def add_lane(hub, name, prefix=None, owners=(), wip=None):
    taken = [x["id"] for x in hub.lanes()] + [hub.prefix]
    pid = (prefix or K.suggest_prefix(name, taken)).upper()
    if not K.PREFIX_RE.match(pid) or pid in taken:
        raise SignalboxError(f"Lane prefix '{pid}' is invalid or taken")
    lane = {"id": pid, "name": name, "owners": list(owners), "wip": wip}
    hub.tracker.ensure_label(LANE_PREFIX + name, "14b8a6", lane_description(lane))
    hub._labels = None
    return lane


def cmd_add_lane(args):
    hub = open_hub(args)
    lane = add_lane(hub, " ".join(args.name.split()), args.prefix, args.owners or (), args.wip)
    print(f"Lane {lane['name']} [{lane['id']}] is on {hub.tracker.describe()} as the label {LANE_PREFIX}{lane['name']}.")


def cmd_depend(args):
    hub = open_hub(args)
    hub.depend(args.id, args.on)
    print(f"{hub.tid(number_of(args.id))} now waits on {', '.join(hub.tid(number_of(x)) for x in args.on)}")


def cmd_undepend(args):
    hub = open_hub(args)
    hub.depend(args.id, args.on, remove=True)
    print(f"{hub.tid(number_of(args.id))} no longer waits on {', '.join(hub.tid(number_of(x)) for x in args.on)}")


def cmd_edit(args):
    hub = open_hub(args)
    desc = text_arg(args, "new_desc", "description") if (args.new_desc is not None or args.stdin or args.file) else None
    hub.edit(args.id, title=args.title, desc=desc, lane=args.lane, kind=args.kind, note=args.message)
    print(f"Updated {hub.tid(number_of(args.id))}")


def cmd_withdraw(args):
    hub = open_hub(args)
    for issue in hub.withdraw(args.ids, args.message or ""):
        print(f"Withdrew {hub.tid(issue['number'])}: {issue['title']}")


def cmd_approve(args):
    hub = open_hub(args)
    for issue in hub.approve(args.ids):
        print(f"Approved {hub.tid(issue['number'])}: {issue['title']}")


def cmd_changes(args):
    hub = open_hub(args)
    hub.request_changes(args.id, text_arg(args, "text", "request"))
    print(f"Asked for changes on {hub.tid(number_of(args.id))}")


def cmd_accept(args):
    hub = open_hub(args)
    for issue in hub.accept(args.ids):
        print(f"Done: {hub.tid(issue['number'])} {issue['title']}")


def cmd_send_back(args):
    hub = open_hub(args)
    hub.send_back(args.id, text_arg(args, "text", "feedback"))
    print(f"Sent {hub.tid(number_of(args.id))} back (review -> todo); the feedback is the first note its next holder reads.")


def cmd_reject(args):
    hub = open_hub(args)
    hub.reject(args.ids, args.message or "")
    print(f"Rejected {', '.join(hub.tid(number_of(x)) for x in args.ids)}")


def cmd_yolo(args):
    hub = open_hub(args)
    hub.set_yolo(args.id, args.state == "on")
    print(f"YOLO {args.state} for {hub.tid(number_of(args.id))}")


def cmd_unlock(args):
    hub = open_hub(args)
    rec = hub.unlock(args.id, args.message or "")
    print(f"Released the lock of {rec.get('label')} on {hub.tid(number_of(args.id))}")


def cmd_locks(args):
    hub = open_hub(args)
    table = hub.locks.table(refresh=True)
    if not table:
        print(f"No interlocks are held ({hub.locks.where()}).")
        return
    for n, (sha, rec) in sorted(table.items()):
        print(f"{hub.tid(n):<8} {lock_line(rec)}")
        if rec.get("worktree"):
            print(f"         {rec['worktree']}")


def cmd_worktrees(args):
    hub = open_hub(args)
    table = hub.locks.table(refresh=True)
    holders = {rec.get("worktree"): (n, rec) for n, (_, rec) in table.items() if rec.get("worktree")}
    rows = [w for w in hub.worktrees.list() if (w["branch"] or "").startswith("sb/") or w["path"] in holders]
    if not rows:
        print("No task worktrees.")
    for w in rows:
        held = holders.get(w["path"])
        who = f"held by {held[1].get('label')}" if held else "no lock (gc removes it once its task is done)"
        dirty = hub.worktrees.dirty(w["path"])
        print(f"{w['path']}\n  {w['branch']} · {who}" + (f" · {len(dirty)} uncommitted file(s)" if dirty else ""))


def cmd_radar(args):
    hub = open_hub(args)
    clashes, files = hub.radar()
    active = len(files)
    if not active:
        print("No work in progress to compare.")
        return
    if not clashes:
        print(f"No overlap between the {active} task(s) in progress.")
        return
    for c in clashes:
        a, b = c["a"], c["b"]
        print(f"{a['id']} ({a['sb']['lock'].get('label')}) and {b['id']} ({b['sb']['lock'].get('label')}):")
        if c["files"]:
            print("  both change: " + ", ".join(c["files"][:10]) + (" ..." if len(c["files"]) > 10 else ""))
        if c["touch"]:
            print("  change files the other declared: " + ", ".join(c["touch"][:10]))
        if c["declared"]:
            print("  declared paths overlap: " + ", ".join(c["declared"]))
    print("\nTalk it over with a note on either task, or have one wait on the other with `depend`.")
    return 1


def cmd_map(args):
    hub = open_hub(args)
    if args.action == "new":
        if not args.title:
            raise SignalboxError("map new needs a title")
        dest = K.read_input(args.destination, args.stdin, args.file, "destination")
        issue = hub.map_new(args.title, dest, args.notes or "", lane=args.lane)
        tid = hub.tid(issue["number"])
        print(f"Charted map {tid}: {issue['title']}" + (" (proposed: a person agrees the destination first)"
                                                        if hub.state_of(issue) == "proposed" else ""))
        print(f"Add its tickets with `{command(f'propose - TITLE --map {tid} --kind grilling')}`, or a batch with \"map\": \"{tid}\".")
    else:
        board, t = hub.task(args.title)
        print_task(board, t)


def cmd_fog(args):
    hub = open_hub(args)
    items = hub.fog(args.map, add=args.add, remove=args.remove)
    print(f"Not yet specified on {hub.tid(number_of(args.map))}:")
    for x in items:
        print(f"  - {x}")
    if not items:
        print("  (clear)")


def cmd_graduate(args):
    hub = open_hub(args)
    hub.fog(args.map, remove=args.fog)
    issue = hub.propose(args.title, args.desc or "", kind=args.kind, map_ref=args.map, deps=args.deps or ())
    print(f"Graduated fog into {hub.tid(issue['number'])}: {issue['title']} [{args.kind}]")


def cmd_scope_out(args):
    hub = open_hub(args)
    hub.scope_out(args.id, args.message)
    print(f"Ruled {hub.tid(number_of(args.id))} out of scope; the map lists it under Out of scope.")


def cmd_sync(args):
    hub = open_hub(args)
    board = hub.write_mirror()
    print(f"Mirrored {len(board['tasks'])} tasks from {hub.tracker.describe()} to {K.rel(hub.mirror_path())}")
    warn(hub)


def cmd_gc(args):
    hub = open_hub(args)
    board = hub.board()
    by_id = K.task_map(board)
    issues = hub.issues()
    acted = []
    for n, (sha, rec) in sorted(hub.locks.table(refresh=True).items()):
        issue = issues.get(n)
        state = hub.state_of(issue) if issue else None
        if state != "doing":
            mine = rec.get("holder") == hub.actor.by
            if mine or (hub.actor.human and hub.interactive) or lease_left(rec) <= 0:
                acted.append(f"release the lock on {hub.tid(n)} ({state or 'gone'})")
                if not args.dry_run:
                    hub.locks.release(n, sha)
                    if issue:
                        hub.tracker.remove_label(n, LOCK_LABEL)
    held = {rec.get("worktree") for _, rec in hub.locks.table(refresh=True).values()}
    base = hub.worktrees.base(hub.locks.remote)
    for w in hub.worktrees.list():
        m = re.match(r"^sb/([a-z][a-z0-9]*-\d+)-", w["branch"] or "")
        if not m or w["path"] in held:
            continue
        t = by_id.get(m.group(1).upper())
        lock = (t or {}).get("sb", {}).get("lock")
        # A worktree left behind by a handoff: someone else holds the task now, elsewhere, on the pushed branch.
        moved_on = lock and lock.get("worktree") != w["path"] and \
            git("rev-parse", "--verify", "--quiet", f"{hub.locks.remote}/{w['branch']}", cwd=hub.project.root, check=False).returncode == 0 \
            if hub.locks.remote else False
        if t and t["state"] != "done" and not moved_on:
            continue
        dirty = hub.worktrees.dirty(w["path"])
        if dirty and not args.force:
            print(f"Keeping {w['path']}: {len(dirty)} uncommitted file(s); --force removes it anyway")
            continue
        acted.append(f"remove worktree {w['path']}")
        if not args.dry_run:
            hub.worktrees.remove(w["path"], force=bool(dirty))
            merged = git("merge-base", "--is-ancestor", w["branch"], base, cwd=hub.project.root, check=False).returncode == 0
            if merged:
                git("branch", "-d", w["branch"], cwd=hub.project.root, check=False)
                acted.append(f"delete merged branch {w['branch']}")
    if not args.dry_run:
        git("worktree", "prune", cwd=hub.project.root, check=False)
    for a in acted:
        print(("Would " if args.dry_run else "") + a)
    if not acted:
        print("Nothing to clean up.")


def cmd_doctor(args):
    hub = open_hub(args)
    ok = True

    def check(good, text, fix=""):
        nonlocal ok
        ok = ok and good
        print(("ok    " if good else "FIX   ") + text + ("" if good or not fix else f"\n      {fix}"))

    check(sys.version_info >= (3, 8), f"Python {sys.version.split()[0]}")
    try:
        me = hub.tracker.whoami()
        check(True, f"hub {hub.tracker.describe()} reachable as @{me}")
        hub.ensure_labels()
        check(True, "Signalbox labels exist on the hub")
    except SignalboxError as e:
        check(False, f"hub {hub.tracker.describe()}: {e}", "Set GH_TOKEN or run `gh auth login`; the token needs Issues read/write.")
    try:
        table = hub.locks.table(refresh=True)
        check(True, f"interlocks readable on {hub.locks.where()} ({len(table)} held)")
        if args.probe:
            probe = hub._record({"number": 0}, branch=None)
            sha = hub.locks.acquire(0, {**probe, "task": "probe"})
            hub.locks.release(0, sha)
            check(True, "interlocks writable: a probe lock was taken and released")
    except SignalboxError as e:
        check(False, f"interlocks on {hub.locks.where()}: {e}", "Signalbox pushes refs/signalbox/locks/*; the remote must accept it.")
    if not hub.locks.remote:
        print("note  locks are local to this repository: agents on this machine share them, other machines do not. "
              "Add an origin remote to share them.")
    try:
        board = hub.board()
        stale = [t["id"] for t in board["tasks"] if t["state"] == "doing" and not t["sb"].get("lock")]
        expired = [t["id"] for t in board["tasks"] if (t["sb"].get("lock") or {}).get("expired")]
        check(not stale, "every task in progress holds an interlock" if not stale else f"doing without a lock: {', '.join(stale)}",
              "Its holder claims it again, or a person moves it back to todo in the board.")
        check(not expired, "no expired leases" if not expired else f"expired leases: {', '.join(expired)}",
              f"The holder runs `{command('heartbeat ID')}`, or someone takes over with `{command('claim ID --take-over')}`.")
    except SignalboxError as e:
        check(False, str(e))
    prunable = [w["path"] for w in hub.worktrees.list() if w.get("prunable")]
    check(not prunable, "worktrees intact" if not prunable else f"missing worktrees: {', '.join(prunable)}", f"`{command('gc')}` prunes them.")
    return 0 if ok else 1


# The guard runs in GitHub Actions on issue events. GitHub knows who made each change, so the guard turns the human-only
# decisions into rules even for clients that do not run Signalbox: an approval, acceptance or YOLO by someone who may
# not decide is undone, with a comment saying why.

def guard_event(hub, event):
    issue_raw = event.get("issue") or {}
    sender = login_of((event.get("sender") or {}).get("login"))
    action = event.get("action")
    if not issue_raw or sender.endswith("[bot]") and sender not in {login_of(x) for x in hub.config["agents"].get("logins", [])}:
        return None
    issue = GitHubTracker._issue(issue_raw)
    if MARK not in issue["labels"]:
        return None
    labels = set(issue["labels"])
    label = ((event.get("label") or {}).get("name"))
    decision = None
    if action == "labeled" and label == STATE_LABELS["todo"] and STATE_LABELS["proposed"] in labels:
        decision = "approve"
    elif action == "labeled" and label == STATE_LABELS["todo"] and STATE_LABELS["review"] in labels:
        decision = "send back"
    elif action == "labeled" and label == YOLO_LABEL:
        decision = "turn YOLO on"
    elif action == "closed" and issue.get("state_reason") == "completed" and STATE_LABELS["doing"] not in labels:
        decision = "mark done"
    elif action == "closed" and issue.get("state_reason") == "completed":
        cover = K.yolo_cover(hub.board())
        via = cover.get(hub.tid(issue["number"]).upper())
        decision = None if via and via.upper() != hub.tid(issue["number"]).upper() else "mark done"
    if not decision:
        return None
    agents = {login_of(x) for x in hub.config["agents"].get("logins", [])}
    allowed = hub.approvers_for(issue)
    if sender not in agents and (not allowed or sender in allowed):
        return None
    n = issue["number"]
    if decision in ("approve", "send back"):
        hub.tracker.remove_label(n, STATE_LABELS["todo"])
    elif decision == "turn YOLO on":
        hub.tracker.remove_label(n, YOLO_LABEL)
    else:
        hub.tracker.update_issue(n, state="open")
        back = STATE_LABELS["doing"] if STATE_LABELS["doing"] in labels else STATE_LABELS["review"]
        hub.tracker.add_labels(n, [back])
    who = "an agent account" if sender in agents else "not an approver here"
    allowed_txt = ", ".join("@" + x for x in sorted(allowed)) or "a person"
    hub.tracker.add_comment(n, render_note(f"Undid **{decision}** by @{sender}, who is {who}. That decision belongs to "
                                           f"{allowed_txt}.", Actor("signalbox-guard", False), "event"))
    return f"undid {decision} by @{sender} on #{n}"


def cmd_guard(args):
    project = Project()
    tracker = make_tracker(project, project.require_config())
    hub = Hub(project, tracker, Actor("signalbox-guard", False))
    event = json.loads(K.read_text_file(args.event or os.environ.get("GITHUB_EVENT_PATH", "")))
    out = guard_event(hub, event)
    print(out or "Nothing to guard in this event.")


# ---------------------------------------------------------------------------
# init: set a project up
# ---------------------------------------------------------------------------

AGENTS_LINE = "Plan, claim and track work on the Signalbox board; follow .agents/skills/signalbox/SKILL.md."
STATE_GITIGNORE = "# Signalbox keeps its local state here; only the config is shared.\n*\n!.gitignore\n!config.json\n"


def cmd_init(args):
    project = Project(args.dir)
    root = project.main
    made = []

    def create(rel_path, text):
        path = os.path.join(root, rel_path)
        if os.path.exists(path):
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        K.write_text_atomic(path, text)
        made.append(rel_path)
        return True

    cfg_rel = os.path.join(STATE_DIR, CONFIG_FILE)
    if not os.path.isfile(os.path.join(root, cfg_rel)):
        repo = None
        if args.hub in (None, "github") or (args.hub or "").startswith("github:"):
            repo = args.hub.split(":", 1)[1] if args.hub and ":" in args.hub else project.github_repo()
            if args.hub and args.hub.startswith("github") and not repo:
                raise SignalboxError("No GitHub origin here; name the repository: --hub github:owner/name")
        hub = {"type": "github", "repo": repo} if repo else {"type": "file", "path": ".signalbox/hub.json"}
        prefix = (args.prefix or K.suggest_prefix(project.name, [])).upper()
        cfg = {"signalbox": FORMAT, "title": args.title or project.name, "prefix": prefix, "hub": hub,
               "approvers": [login_of(a) for a in args.approvers or []], "teams": {},
               "lanes": [], "agents": DEFAULT_CONFIG["agents"], "locks": DEFAULT_CONFIG["locks"],
               "worktrees": DEFAULT_CONFIG["worktrees"], "paths": "warn", "poll_seconds": 15}
        create(cfg_rel, json.dumps(cfg, indent=2) + "\n")
    create(os.path.join(STATE_DIR, ".gitignore"), STATE_GITIGNORE)

    # The agent skill: instructions plus the project's own copy of Signalbox, so agents need nothing installed.
    skill_dir = os.path.join(root, ".agents", "skills", "signalbox")
    os.makedirs(skill_dir, exist_ok=True)
    for src, name in ((os.path.abspath(__file__), "signalbox.py"), (os.path.abspath(K.__file__), "kanvas.py"),
                      (K.HTML_FILE, "kanvas.html"), (SKILL_FILE, "SKILL.md")):
        dst = os.path.join(skill_dir, name)
        if os.path.isfile(src) and not K.same_file(src, dst):
            shutil.copyfile(src, dst)
    claude = os.path.join(root, ".claude", "skills", "signalbox", "SKILL.md")
    os.makedirs(os.path.dirname(claude), exist_ok=True)
    if os.path.isfile(SKILL_FILE) and not K.same_file(SKILL_FILE, claude):
        shutil.copyfile(SKILL_FILE, claude)
    agents_md = os.path.join(root, "AGENTS.md")
    if not create("AGENTS.md", f"# Agents\n\n{AGENTS_LINE}\n"):
        if "skills/signalbox" not in K.read_text_file(agents_md):
            print(f"Add this line to AGENTS.md so every agent finds the board:\n  {AGENTS_LINE}")
    project = Project(root)
    if project.config["hub"]["type"] == "github" and os.path.isfile(GUARD_FILE):
        with open(GUARD_FILE, encoding="utf-8") as f:
            create(os.path.join(".github", "workflows", "signalbox-guard.yml"), f.read())
    for m in made:
        print(f"Created {m}")
    print(f"Installed the agent skill in .agents/skills/signalbox/ (and .claude/skills/signalbox/ for Claude Code)")
    hub = Hub(project, make_tracker(project, project.config), interactive=person_present())
    try:
        hub.ensure_labels()
        print(f"Hub {hub.tracker.describe()} is ready (labels created).")
    except SignalboxError as e:
        print(f"Could not prepare the labels on {hub.tracker.describe()} yet: {e}\nRun `{command('doctor')}` once you have access.")
    print(f"\nNext: commit {STATE_DIR}/config.json, .agents/ and AGENTS.md, then run `{command('serve')}` and ask your agent "
          "to plan the work with you.")


# ---------------------------------------------------------------------------
# serve: the shared board in the browser
# ---------------------------------------------------------------------------


class SignalboxHandler(K.BoardHandler):
    """Kanvas's board server, with saves replayed as hub operations and guarded by a key.

    A save acts as the person who ran `serve`, with their authority to approve and accept. Plain Kanvas lets anything
    on this computer save; here that would let an agent on the same machine approve its own work with one request.
    So `serve` makes a random key per run and puts it in the link it opens and prints. Opening that link stores the
    key in an HttpOnly cookie for this board and port, and redirects to the board without it; every save must carry
    the cookie. Reading the board needs no key: it holds nothing the hub does not already show."""

    server_version = "Signalbox/" + VERSION

    def _cookie_name(self):
        # Cookies ignore ports, so the name carries it: two servers on one computer keep their own keys.
        return f"signalbox_key_{self.server.server_address[1]}"

    def _has_key(self):
        want = getattr(self.server.main, "key", None)
        if not want:
            return True
        try:
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return False
        got = jar.get(self._cookie_name())
        return got is not None and hmac.compare_digest(got.value.encode(), want.encode())

    def do_GET(self):
        parts = self._route()
        query = parse_qs(urlparse(self.path).query)
        if "key" in query and self._host_allowed() and len(parts) == 2 and parts[1] in ("", "index.html") \
                and parts[0] in self.server.main.hubs:
            want = getattr(self.server.main, "key", "") or ""
            if not hmac.compare_digest(query["key"][0].encode(), want.encode()):
                return self._error(403, "This link's key is not this server's; open the link `signalbox serve` printed "
                                        "when it started")
            board = f"/{quote(parts[0])}/"
            cookie = f"{self._cookie_name()}={want}; Path={board}; HttpOnly; SameSite=Strict"
            return self._send(302, headers={"Location": board, "Set-Cookie": cookie})
        return super().do_GET()

    def do_POST(self):
        # Kanvas opens boards to phones through a second listener with its own plain handler, whose saves would
        # write the mirror directly, past the hub's rules and the key. Signalbox boards stay on this computer.
        if self._route()[1:] == ["api", "phone"]:
            return self._error(403, "Signalbox boards are not opened to phones; use the board on this computer")
        return super().do_POST()

    def do_PUT(self):
        if not self._host_allowed():
            return self._error(403, "Host not allowed")
        parts = self._route()
        entry = self.server.main.hubs.get(parts[0]) if parts[1:] == ["api", "board"] else None
        if entry is None:
            return self._error(404, "Not found")
        if not self._has_key():
            return self._error(403, "Saving needs this server's key: open the link `signalbox serve` printed when it "
                                    "started (it ends in ?key=…), then make the change again")
        hub, lock = entry
        text = self._read_json()
        if text is None:
            return
        expected = self.headers.get("If-Match")
        if not expected:
            return self._error(428, "Expected If-Match with the ETag of the board this changes; load the board first")
        try:
            new = K.parse_board(text)
            if "tasks" not in json.loads(text):
                raise K.KanvasError("Expected a board with its task list")
        except K.KanvasError as e:
            return self._error(400, str(e))
        path = hub.mirror_path()
        with lock:
            with open(path, "rb") as f:
                current = f.read()
            tag = K.etag_of(current)
            if expected != tag:
                return self._send(409, current, headers={"ETag": tag})
            old = K.parse_board(current.decode("utf-8"))
            error = None
            hub.invalidate()
            hub.locks._raw = None
            try:
                done = hub.apply_board(old, new)
                if done and not self.server.quiet:
                    print(f"[{time.strftime('%H:%M:%S')}] {hub.actor.label} in the browser: {'; '.join(done)}", flush=True)
            except K.KanvasError as e:
                error = str(e)
            # Board-level layout keys (manual order, folds) belong to this browser; keep them in the mirror.
            hub.invalidate()
            board = hub.write_mirror()
            if not error:
                extra = {k: v for k, v in new.items() if k not in ("kanvas", "title", "groups", "tasks", "signalbox")}
                if extra:
                    board.update(extra)
                    K.write_text_atomic(path, K.dumps(board))
            with open(path, "rb") as f:
                data = f.read()
        if error:
            return self._error(403, error)
        self._send(200, data, headers={"ETag": K.etag_of(data)})


class SignalboxServer(K.BoardServer):
    handler_class = SignalboxHandler

    def __init__(self, *a, **kw):
        self.hubs = {}
        self.key = secrets.token_urlsafe(24)
        super().__init__(*a, **kw)


def poll_hub(server, name, hub, lock, every):
    """Refresh the mirror every `every` seconds. A failure is reported once, not every round while it lasts, and
    the first refresh that works again says so."""
    failing = None
    while True:
        time.sleep(every)
        try:
            with lock:
                hub.invalidate()
                hub.locks._raw = None
                hub.write_mirror()
        except Exception as e:  # keep serving through network blips
            text = str(e)
            if text != failing and not server.quiet:
                more = " (said once while it lasts)" if failing is None else ""
                print(f"[{time.strftime('%H:%M:%S')}] could not refresh {name}: {text}{more}", file=sys.stderr, flush=True)
            failing = text
            continue
        if failing is not None and not server.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] refreshing {name} works again", flush=True)
        failing = None


def cmd_serve(args):
    project = Project()
    project.require_config()
    tracker = make_tracker(project, project.config)
    hub = Hub(project, tracker, Actor(safe_whoami(tracker), True), interactive=True)
    board = hub.write_mirror()
    hosts = K.LOCAL_NAMES if args.host in ("127.0.0.1", "localhost") else None
    server, last = None, None
    for port in range(args.port, args.port + 20):
        try:
            server = SignalboxServer((args.host, port), None, hosts, False)
            break
        except OSError as e:
            last = e
    if server is None:
        raise SignalboxError(f"No free port from {args.port}: {last}")
    name = server.add_board(hub.mirror_path(), save=False)
    lock = threading.Lock()
    server.hubs[name] = (hub, lock)
    threading.Thread(target=poll_hub, args=(server, name, hub, lock, max(2, int(args.poll or project.config["poll_seconds"]))),
                     daemon=True).start()
    host, port = server.server_address[:2]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/{quote(name)}/?key={server.key}"
    print(f"Signalbox board for {board['title']}: {len(board['tasks'])} tasks from {tracker.describe()}, "
          f"locks on {hub.locks.where()}")
    print(f"You act as {hub.actor.label} in the browser.")
    print(f"Open {url}  (Ctrl+C to stop)")
    print("Saving from the browser needs the key in this link; keep it to yourself, and away from your agents.")
    if not server.check_host:
        print("Warning: listening beyond localhost; anyone with this link who can reach this port acts as you on the board.")
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


def cmd_export(args):
    hub = open_hub(args)
    hub.write_mirror()
    with open(K.HTML_FILE, encoding="utf-8") as f:
        html = f.read()
    data = K.dumps(K.load_board(hub.mirror_path())).replace("</", "<\\/")
    if K.SNAPSHOT_TAG not in html:
        raise SignalboxError("kanvas.html has no snapshot slot")
    out = html.replace(K.SNAPSHOT_TAG, K.SNAPSHOT_TAG.replace("></script>", f">{data}</script>"), 1)
    K.write_text_atomic(args.output, out)
    print(f"Wrote a read-only snapshot to {args.output}")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

EPILOG = """\
Workflow:  proposed -> todo -> doing -> review -> done
  agents:  propose, claim (lock + worktree), heartbeat, note, ask, finish/resolve, pause, handoff
  people:  approve, changes, accept, send-back, reject, yolo, unlock (or do it all in `serve`)

Agents sign with --as NAME or $SIGNALBOX_AGENT and act for $SIGNALBOX_SPONSOR (default: the hub login).
"""


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="as_name", default=argparse.SUPPRESS, metavar="NAME",
                        help="act as this agent (default: $SIGNALBOX_AGENT; unset means you are a person)")
    prog = "signalbox" if INSTALLED else os.path.basename(sys.argv[0]) or "signalbox.py"
    parser = argparse.ArgumentParser(prog=prog, parents=[common], epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     description="Signalbox: the interlocking board for teams of humans and AI agents.")
    parser.add_argument("--version", action="version", version=f"Signalbox {VERSION} (Kanvas {K.VERSION})")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, func, help_text, aliases=()):
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text, aliases=list(aliases))
        p.set_defaults(func=func)
        return p

    def text_sources(p, what):
        p.add_argument("--stdin", action="store_true", help=f"read the {what} from stdin (UTF-8)")
        p.add_argument("--file", metavar="PATH", help=f"read the {what} from a file")

    add("status", cmd_status, "the board, its interlocks and maps (default command)")
    p = add("list", cmd_list, "tasks by lane or map, optionally filtered by state, kind or lane")
    p.add_argument("filter", nargs="?")
    p = add("show", cmd_show, "a task's details, lock, dependencies and notes")
    p.add_argument("id")
    add("ready", cmd_ready, "tasks anyone can claim now")
    p = add("frontier", cmd_frontier, "the takeable edge of a map: open, unblocked, unclaimed tickets")
    p.add_argument("map", nargs="?")
    add("blocked", cmd_blocked, "blocked tasks and what they wait on")
    add("check", cmd_check, "validate the board (exit code 1 on errors)")
    add("whoami", cmd_whoami, "who you act as, and for whom")

    p = add("claim", cmd_claim, "lock a ready task, move it to doing, and create its worktree", aliases=("start",))
    p.add_argument("id")
    p.add_argument("-m", "--message", help="your plan, kept as a note")
    p.add_argument("--paths", nargs="+", metavar="GLOB", help="files you expect to change, for the overlap radar")
    p.add_argument("--with", dest="pair", metavar="LOGIN", help="the person you work a HITL ticket with")
    p.add_argument("--no-worktree", action="store_true", help="lock the task only")
    p.add_argument("--take-over", action="store_true", help="take over a lock whose lease ran out (a person may break a live one)")
    text_sources(p, "plan")
    p = add("heartbeat", cmd_heartbeat, "renew your lease on a task")
    p.add_argument("id")
    p = add("finish", cmd_finish, "push the branch, hand the task over for review, release the lock")
    p.add_argument("id")
    p.add_argument("-m", "--message", help="summary for the reviewer")
    p.add_argument("--pr", action="store_true", help="also open a pull request in the hub repository")
    text_sources(p, "summary")
    p = add("resolve", cmd_resolve, "answer a decision ticket (research, prototype, grilling, task)")
    p.add_argument("id")
    p.add_argument("-m", "--message", help="the answer")
    text_sources(p, "answer")
    p = add("pause", cmd_pause, "stop, push the branch, release the lock (doing -> todo)")
    p.add_argument("id")
    p.add_argument("-m", "--message")
    text_sources(p, "note")
    p = add("handoff", cmd_handoff, "pause and hand the task to someone, who resumes the pushed branch")
    p.add_argument("id")
    p.add_argument("--to", required=True, metavar="LOGIN")
    p.add_argument("-m", "--message")
    text_sources(p, "note")
    p = add("note", cmd_note, "add a note (a comment on the issue)")
    p.add_argument("id")
    p.add_argument("text", nargs="?")
    text_sources(p, "note")
    p = add("ask", cmd_ask, "show that you wait for a person's answer in your conversation")
    p.add_argument("id")
    p.add_argument("text", nargs="?")
    text_sources(p, "question")
    p = add("propose", cmd_propose, "propose a task (people add it as approved)")
    p.add_argument("lane", help="lane name or prefix, or - for none")
    p.add_argument("title")
    p.add_argument("desc", nargs="?")
    p.add_argument("--deps", nargs="+", metavar="ID")
    p.add_argument("--kind", default="build", choices=KINDS)
    p.add_argument("--map", metavar="ID", help="the map this ticket belongs to")
    p.add_argument("--paths", nargs="+", metavar="GLOB")
    text_sources(p, "description")
    p = add("batch", cmd_batch, "propose many lanes and tasks from JSON on stdin or --file")
    p.add_argument("--file", metavar="PATH")
    p = add("add-lane", cmd_add_lane, "create a lane (a team or area of work)")
    p.add_argument("name")
    p.add_argument("--prefix")
    p.add_argument("--owners", nargs="+", metavar="LOGIN")
    p.add_argument("--wip", type=int)
    p = add("depend", cmd_depend, "make a task wait on others")
    p.add_argument("id")
    p.add_argument("on", nargs="+")
    p = add("undepend", cmd_undepend, "remove dependencies (agents: proposals only)")
    p.add_argument("id")
    p.add_argument("on", nargs="+")
    p = add("edit", cmd_edit, "revise a proposal (people: any task)")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--lane")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--desc", dest="new_desc", metavar="DESC")
    p.add_argument("-m", "--message", help="a note on what changed")
    text_sources(p, "description")
    p = add("withdraw", cmd_withdraw, "close proposals that are not approved yet")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.add_argument("-m", "--message")

    p = add("approve", cmd_approve, "approve proposals (people, at a terminal)")
    p.add_argument("ids", nargs="+", metavar="ID")
    p = add("changes", cmd_changes, "ask for changes to a proposal")
    p.add_argument("id")
    p.add_argument("text", nargs="?")
    text_sources(p, "request")
    p = add("accept", cmd_accept, "accept reviewed work (review -> done)", aliases=("done",))
    p.add_argument("ids", nargs="+", metavar="ID")
    p = add("send-back", cmd_send_back, "send reviewed work back with feedback (review -> todo)")
    p.add_argument("id")
    p.add_argument("text", nargs="?")
    text_sources(p, "feedback")
    p = add("reject", cmd_reject, "close tasks as not planned")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.add_argument("-m", "--message")
    p = add("yolo", cmd_yolo, "hand a chain to the agents: everything it depends on skips review")
    p.add_argument("id")
    p.add_argument("state", choices=["on", "off"])
    p = add("unlock", cmd_unlock, "release a lock (your own; a person may break anyone's)")
    p.add_argument("id")
    p.add_argument("-m", "--message")

    p = add("map", cmd_map, "Wayfinder maps: `map new TITLE --destination TEXT`, `map show ID`")
    p.add_argument("action", choices=["new", "show"])
    p.add_argument("title", nargs="?", help="title (new) or map ID (show)")
    p.add_argument("--destination", help="what reaching the end of the map looks like")
    p.add_argument("--notes", help="domain, skills to consult, standing preferences")
    p.add_argument("--lane")
    text_sources(p, "destination")
    p = add("fog", cmd_fog, "list, add or clear a map's Not yet specified")
    p.add_argument("map")
    p.add_argument("--add", metavar="TEXT")
    p.add_argument("--remove", metavar="MATCH")
    p = add("graduate", cmd_graduate, "turn a patch of fog into a ticket")
    p.add_argument("map")
    p.add_argument("fog", help="text matching the fog bullet")
    p.add_argument("title")
    p.add_argument("desc", nargs="?")
    p.add_argument("--kind", default="grilling", choices=DECISION_KINDS)
    p.add_argument("--deps", nargs="+", metavar="ID")
    p = add("scope-out", cmd_scope_out, "rule a map ticket out of scope")
    p.add_argument("id")
    p.add_argument("-m", "--message", required=True)

    add("locks", cmd_locks, "every interlock held, by whom, until when")
    add("worktrees", cmd_worktrees, "task worktrees and who holds them")
    add("radar", cmd_radar, "work in progress that touches the same files (exit 1 on overlap)")
    p = add("gc", cmd_gc, "release locks of finished tasks, remove their worktrees and merged branches")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="also remove worktrees with uncommitted changes")
    p = add("doctor", cmd_doctor, "check access, labels, interlocks and invariants")
    p.add_argument("--probe", action="store_true", help="also take and release a probe lock")
    add("sync", cmd_sync, f"refresh the local mirror ({STATE_DIR}/{MIRROR_FILE}) from the hub")
    p = add("serve", cmd_serve, "the shared board in your browser")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--poll", type=int, help="seconds between hub refreshes (default: poll_seconds in the config)")
    p.add_argument("--no-open", action="store_true")
    p = add("export", cmd_export, "write a read-only HTML snapshot")
    p.add_argument("output", nargs="?", default="signalbox-snapshot.html")
    p = add("guard", cmd_guard, "GitHub Actions: undo human-only decisions made by agents or non-approvers")
    p.add_argument("--event", metavar="PATH", help="event JSON (default: $GITHUB_EVENT_PATH)")
    p = add("init", cmd_init, "set Signalbox up in this repository (never overwrites files)")
    p.add_argument("dir", nargs="?")
    p.add_argument("--hub", help="github, github:owner/name, or file (default: github when origin is on GitHub)")
    p.add_argument("--prefix", help="task ID prefix (default: from the repository name)")
    p.add_argument("--title")
    p.add_argument("--approvers", nargs="+", metavar="LOGIN", help="people who approve and accept (default: anyone)")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    for name in ("text", "desc"):
        if extra and getattr(args, name, False) is None and not extra[0].startswith("-"):
            setattr(args, name, extra.pop(0))
    if extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    func = getattr(args, "func", cmd_status)
    try:
        return func(args) or 0
    except BrokenPipeError:  # the reader stopped early, as `| head` does
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except Locked as e:
        print(f"Locked: {e}", file=sys.stderr)
        return 3
    except K.KanvasError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
