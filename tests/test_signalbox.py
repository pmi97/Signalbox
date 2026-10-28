"""Tests for Signalbox: the GitHub-backed, interlocked board on top of Kanvas.

Every test runs against real git repositories in a temporary folder: a bare repository plays the shared remote, and
clones play the machines of different people. The hub is the file tracker, which stores GitHub-shaped issues in one
JSON file; GitHubTracker is checked separately against a fake REST transport.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import kanvas as K  # noqa: E402
import signalbox as S  # noqa: E402

SCRIPT = os.path.join(ROOT, "signalbox.py")
HAS_GIT = shutil.which("git") is not None


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@contextlib.contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("SIGNALBOX_", "KANVAS_", "GH_", "GITHUB_"))}


@unittest.skipUnless(HAS_GIT, "git is not installed")
class World(unittest.TestCase):
    """A shared remote, a file hub, and clones for alice and bob."""

    config = {}

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="signalbox-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = os.path.join(self.tmp, "remote.git")
        self.hub_path = os.path.join(self.tmp, "hub.json")
        sh("git", "init", "--quiet", "--bare", self.remote)
        sh("git", "symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self.alice = self.clone("alice", first=True)
        self.bob = self.clone("bob")

    def clone(self, name, first=False):
        path = os.path.join(self.tmp, name)
        sh("git", "clone", "--quiet", self.remote, path)
        sh("git", "config", "user.email", f"{name}@example.com", cwd=path)
        sh("git", "config", "user.name", name, cwd=path)
        if first:
            sh("git", "checkout", "--quiet", "-b", "main", cwd=path)
            cfg = {"signalbox": 1, "title": "Acme", "prefix": "SB", "hub": {"type": "file", "path": self.hub_path},
                   "lanes": [{"name": "Platform", "prefix": "PL", "owners": ["alice"]}, {"name": "Docs", "prefix": "DC"}]}
            cfg.update(self.config)
            os.makedirs(os.path.join(path, ".signalbox"))
            with open(os.path.join(path, ".signalbox", "config.json"), "w") as f:
                json.dump(cfg, f, indent=2)
            with open(os.path.join(path, ".signalbox", ".gitignore"), "w") as f:
                f.write(S.STATE_GITIGNORE)
            with open(os.path.join(path, "README.md"), "w") as f:
                f.write("acme\n")
            sh("git", "add", "-A", cwd=path)
            sh("git", "commit", "--quiet", "-m", "init", cwd=path)
            sh("git", "push", "--quiet", "-u", "origin", "main", cwd=path)
            sh("git", "remote", "set-head", "origin", "main", cwd=path)
        else:
            sh("git", "remote", "set-head", "origin", "main", cwd=path)
        return path

    def sb(self, *argv, at=None, user="alice", agent=None, tty=False, stdin=None, code=0):
        env = dict(CLEAN_ENV, SIGNALBOX_USER=user, SIGNALBOX_TEST_TTY="1" if tty else "0")
        if agent:
            env["SIGNALBOX_AGENT"] = agent
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), chdir(at or self.alice), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO((stdin or "").encode()))):
            got = S.main(list(argv))
        text = out.getvalue() + err.getvalue()
        if code is not None:
            self.assertEqual(got, code, text)
        return text

    def hub(self, at=None, user="alice", agent=None, tty=False):
        env = dict(CLEAN_ENV, SIGNALBOX_USER=user, SIGNALBOX_TEST_TTY="1" if tty else "0")
        if agent:
            env["SIGNALBOX_AGENT"] = agent
        with mock.patch.dict(os.environ, env, clear=True), chdir(at or self.alice):
            project = S.Project()
            tracker = S.make_tracker(project, project.config)
            actor = S.resolve_actor(type("A", (), {})(), tracker)
            return S.Hub(project, tracker, actor, interactive=tty)

    def board(self):
        return self.hub().board()

    def state(self, tid):
        return K.task_map(self.board())[tid.upper()]["state"]

    def plan(self):
        """Three approved tasks: SB-1, SB-2 waiting on SB-1, and SB-3."""
        spec = {"tasks": [{"title": "Design the auth API", "lane": "Platform", "paths": ["src/api/**"]},
                          {"title": "Implement auth", "lane": "Platform", "deps": ["Design the auth API"]},
                          {"title": "Write the docs", "lane": "Docs"}]}
        self.sb("batch", agent="claude-code", stdin=json.dumps(spec))
        self.sb("approve", "SB-1", "SB-2", "SB-3", tty=True)


class Pieces(unittest.TestCase):
    def test_body_keeps_description_and_meta_apart(self):
        body = S.join_body("Do it\n\nwell", {"deps": [3, 1], "paths": ["a/-->b"], "map": None})
        self.assertNotIn("a/-->b", body)  # escaped, so it cannot close the comment
        desc, meta = S.split_body(body)
        self.assertEqual(desc, "Do it\n\nwell")
        self.assertEqual(meta, {"deps": [3, 1], "paths": ["a/-->b"]})
        self.assertEqual(S.split_body("plain"), ("plain", {}))
        # Text appended after the block (a bot's footer, a person's edit) stays in the description.
        desc, meta = S.split_body(body + "\n\n---\n_footer_")
        self.assertEqual((desc, meta["deps"]), ("Do it\n\nwell\n\n---\n_footer_", [3, 1]))

    def test_notes_carry_their_author(self):
        agent = S.Actor("claude-code", False, "alice")
        body = S.render_note("Line one\n\nLine two", agent, "summary")
        note = S.parse_note({"id": 7, "body": body, "user": "alice", "created_at": "2026-10-28T10:00:00Z",
                             "updated_at": "2026-10-28T10:00:00Z"})
        self.assertEqual((note["by"], note["text"], note["kind"], note["cid"]), ("claude-code", "Line one\n\nLine two", "summary", 7))
        plain = S.parse_note({"id": 8, "body": "LGTM", "user": "bob", "created_at": "x", "updated_at": "x"})
        self.assertEqual((plain["by"], plain["text"]), ("@bob", "LGTM"))
        self.assertTrue(K.is_human("@bob") and K.is_human("human") and not K.is_human("codex"))

    def test_map_sections(self):
        body = S.map_template("A spec", "Use the glossary")
        self.assertEqual(S.section(body, "Destination"), "A spec")
        body = S.set_section(body, "Not yet specified", "- refunds\n- taxes")
        self.assertEqual(S.bullets(S.section(body, "Not yet specified")), ["refunds", "taxes"])
        self.assertEqual(S.section(body, "Notes"), "Use the glossary")

    def test_path_overlap(self):
        self.assertTrue(S.paths_overlap("src/**", "src/api/x.py"))
        self.assertTrue(S.paths_overlap("src/api/*.py", "src/api/x.py"))
        self.assertTrue(S.paths_overlap("src/api", "src/api/**"))
        self.assertFalse(S.paths_overlap("docs/**", "src/**"))

    def test_task_refs(self):
        self.assertEqual([S.number_of(x) for x in ("SB-12", "#12", "12", "pl-12")], [12] * 4)
        with self.assertRaises(S.SignalboxError):
            S.number_of("twelve")


class Interlocks(World):
    def test_local_locks_are_compare_and_swap(self):
        lock = S.Interlock(S.Project(self.alice), None)
        sha = lock.acquire(5, {"task": "SB-5", "holder": "a", "label": "a"})
        with self.assertRaises(S.Locked) as cm:
            lock.acquire(5, {"task": "SB-5", "holder": "b"})
        self.assertEqual(cm.exception.record["holder"], "a")
        with self.assertRaises(S.Locked):
            lock.replace(5, "1" * 40, {"task": "SB-5", "holder": "b"})
        new = lock.replace(5, sha, {"task": "SB-5", "holder": "a", "beat": 1})
        self.assertEqual(lock.get(5)[1]["beat"], 1)
        lock.release(5, new)
        self.assertEqual(lock.get(5), (None, None))
        # The ref is append-only: released means a "free" record on top, and the next claim builds on it.
        again = lock.acquire(5, {"task": "SB-5", "holder": "b"})
        self.assertEqual(lock.get(5)[1]["holder"], "b")
        self.assertEqual(sh("git", "rev-list", "--count", again, cwd=self.alice), "4")  # claim, renew, free, claim

    def test_remote_locks_never_delete_or_force(self):
        # A remote that refuses deletions and non-fast-forward updates, as protected branches and some proxies do.
        sh("git", "config", "receive.denyDeletes", "true", cwd=self.remote)
        sh("git", "config", "receive.denyNonFastForwards", "true", cwd=self.remote)
        lock = S.Interlock(S.Project(self.alice), "origin")
        sha = lock.acquire(7, {"task": "SB-7", "holder": "a"})
        sha = lock.replace(7, sha, {"task": "SB-7", "holder": "a", "beat": 1})
        lock.release(7, sha)
        theirs = S.Interlock(S.Project(self.bob), "origin")
        self.assertEqual(theirs.get(7), (None, None))
        theirs.acquire(7, {"task": "SB-7", "holder": "b"})
        with self.assertRaises(S.Locked):
            lock.acquire(7, {"task": "SB-7", "holder": "a"})

    def test_exactly_one_of_two_machines_wins_a_race(self):
        for round_ in range(4):
            n = 100 + round_
            results = {}

            def claim(who, path):
                p = subprocess.run([sys.executable, "-c", (
                    "import sys; sys.path.insert(0, %r); import signalbox as S\n"
                    "lock = S.Interlock(S.Project(), 'origin')\n"
                    "try:\n    lock.acquire(%d, {'task': 'T', 'holder': %r}); print('won')\n"
                    "except S.Locked as e:\n    print('lost', e.record.get('holder'))") % (ROOT, n, who)],
                    cwd=path, capture_output=True, text=True, env=CLEAN_ENV)
                results[who] = p.stdout.strip() or p.stderr

            threads = [threading.Thread(target=claim, args=(w, p)) for w, p in (("alice", self.alice), ("bob", self.bob))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            outcomes = sorted(v.split()[0] for v in results.values())
            self.assertEqual(outcomes, ["lost", "won"], results)
            winner = next(w for w, v in results.items() if v == "won")
            loser = next(v for v in results.values() if v != "won")
            self.assertEqual(loser, f"lost {winner}")
        # Both machines read the same locks from the remote.
        self.assertEqual(sorted(S.Interlock(S.Project(self.bob), "origin").table()), [100, 101, 102, 103])


class Lifecycle(World):
    def test_claim_work_finish_review_accept(self):
        self.plan()
        out = self.sb("claim", "SB-1", "-m", "Plan: sketch the endpoints", agent="claude-code")
        self.assertIn("Claimed SB-1", out)
        wt = os.path.join(self.tmp, "alice.worktrees", "SB-1")
        self.assertTrue(os.path.isdir(wt))
        brief = read(os.path.join(wt, ".signalbox", "TASK.md"))
        self.assertIn("Plan: sketch the endpoints", brief)
        self.assertIn("SB_PORT=4101", read(os.path.join(wt, ".signalbox", "env")))
        listing = sh("git", "worktree", "list", "--porcelain", cwd=self.alice)
        self.assertIn("locked signalbox: SB-1 held by claude-code (for @alice)", listing)

        board = self.board()
        t = K.task_map(board)["SB-1"]
        self.assertEqual((t["state"], t["sb"]["lock"]["holder"], t["sb"]["lock"]["branch"]),
                         ("doing", "claude-code", "sb/sb-1-design-the-auth-api"))
        self.assertEqual(K.problems(board)[0], [])

        # Another agent, on another machine, cannot take it.
        self.assertIn("is locked by claude-code (for @alice)", self.sb("claim", "SB-1", at=self.bob, user="bob", agent="codex", code=3))
        # Only the holder finishes, with a summary.
        self.assertIn("is held by claude-code", self.sb("finish", "SB-1", "-m", "x", at=self.bob, user="bob", agent="codex", code=3))
        self.assertIn("needs a summary", self.sb("finish", "SB-1", agent="claude-code", code=1))

        os.makedirs(os.path.join(wt, "src", "api"))
        with open(os.path.join(wt, "src", "api", "auth.md"), "w") as f:
            f.write("POST /login\n")
        sh("git", "add", "-A", cwd=wt)
        sh("git", "commit", "--quiet", "-m", "design", cwd=wt)
        out = self.sb("finish", "SB-1", "-m", "Endpoints are in src/api/auth.md", agent="claude-code")
        self.assertIn("doing -> review", out)
        self.assertIn("sb/sb-1-design-the-auth-api", sh("git", "ls-remote", "--heads", "origin", cwd=self.alice))
        self.assertEqual(self.state("SB-1"), "review")
        self.assertNotIn("lock", K.task_map(self.board())["SB-1"]["sb"])

        # Review belongs to people, at a terminal, who may decide in that lane.
        self.assertIn("Only a person", self.sb("accept", "SB-1", agent="claude-code", code=1))
        self.assertIn("human decision", self.sb("accept", "SB-1", code=1))
        self.assertIn("cannot mark done", self.sb("accept", "SB-1", at=self.bob, user="bob", tty=True, code=1))
        self.sb("accept", "SB-1", tty=True)
        self.assertEqual(self.state("SB-1"), "done")
        self.assertIn("SB-2", self.sb("ready", agent="claude-code"))

        # gc removes the finished worktree and, once merged, its branch.
        out = self.sb("gc", agent="claude-code")
        self.assertIn("remove worktree", out)
        self.assertFalse(os.path.isdir(wt))

    def test_proposals_and_approvals(self):
        out = self.sb("propose", "Platform", "Rate-limit login", "Stop brute force", agent="claude-code")
        self.assertIn("Proposed SB-1", out)
        self.assertIn("is a proposal", self.sb("claim", "SB-1", agent="claude-code", code=1))
        self.sb("edit", "SB-1", "--title", "Rate-limit the login endpoint", agent="claude-code")
        self.sb("changes", "SB-1", "Also cover password reset", tty=True)
        self.assertIn("changes requested", self.sb("status", agent="claude-code"))
        self.sb("edit", "SB-1", "--desc", "Login and reset", "-m", "Covered reset too", agent="claude-code")
        self.assertNotIn("changes requested", self.sb("status", agent="claude-code"))
        self.sb("approve", "SB-1", tty=True)
        self.assertIn("only a person changes approved", self.sb("edit", "SB-1", "--title", "x", agent="claude-code", code=1))
        self.sb("propose", "Docs", "Throwaway", agent="claude-code")
        self.sb("withdraw", "SB-2", agent="claude-code")
        self.assertNotIn("SB-2", K.task_map(self.board()))
        added = self.sb("propose", "Docs", "Human task")
        self.assertIn("Added SB-3", added)
        self.assertEqual(self.state("SB-3"), "todo")

    def test_dependencies_refuse_cycles(self):
        self.plan()
        self.assertIn("cycle", self.sb("depend", "SB-1", "SB-2", agent="claude-code", code=1))
        self.sb("depend", "SB-3", "SB-2", agent="claude-code")
        self.assertEqual(K.view_state(K.task_map(self.board())["SB-3"], K.task_map(self.board())), "blocked")
        self.assertIn("Only a person removes", self.sb("undepend", "SB-3", "SB-2", agent="claude-code", code=1))
        self.sb("undepend", "SB-3", "SB-2")

    def test_lease_expiry_and_take_over(self):
        self.plan()
        hub = self.hub(agent="claude-code")
        hub.config["agents"]["lease_minutes"] = -5  # born expired: the holder vanished
        hub.claim("SB-3", worktree=False)
        out = self.sb("claim", "SB-3", "--no-worktree", at=self.bob, user="bob", agent="codex", code=3)
        self.assertIn("lease ran out", out)
        out = self.sb("claim", "SB-3", "--no-worktree", "--take-over", at=self.bob, user="bob", agent="codex")
        self.assertIn("taking over from claude-code (for @alice)", out)
        notes = [n["text"] for n in K.task_map(self.board())["SB-3"]["notes"]]
        self.assertTrue(any("Took over from claude-code" in n for n in notes), notes)

    def test_live_locks_are_broken_only_by_people(self):
        self.plan()
        self.sb("claim", "SB-3", "--no-worktree", agent="claude-code")
        self.assertIn("only a person can break", self.sb("claim", "SB-3", "--take-over", at=self.bob, user="bob", agent="codex", code=3))
        self.assertIn("human decision", self.sb("unlock", "SB-3", at=self.bob, user="bob", code=1))
        self.sb("unlock", "SB-3", "-m", "agent crashed", at=self.bob, user="bob", tty=True)
        self.assertEqual(self.state("SB-3"), "todo")

    def test_handoff_resumes_the_pushed_branch_on_another_machine(self):
        self.plan()
        self.sb("claim", "SB-3", at=self.bob, user="bob", agent="codex")
        wt = os.path.join(self.tmp, "bob.worktrees", "SB-3")
        with open(os.path.join(wt, "guide.md"), "w") as f:
            f.write("half\n")
        sh("git", "add", "-A", cwd=wt)
        sh("git", "commit", "--quiet", "-m", "half", cwd=wt)
        self.sb("handoff", "SB-3", "--to", "alice", "-m", "Out of tokens", at=self.bob, user="bob", agent="codex")
        t = K.task_map(self.board())["SB-3"]
        self.assertEqual((t["state"], t["sb"]["assignees"]), ("todo", ["alice"]))
        out = self.sb("claim", "SB-3", agent="claude-code")
        self.assertIn("resumed from the existing branch", out)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "alice.worktrees", "SB-3", "guide.md")))
        # Bob's old worktree is stale now: gc there removes it.
        self.assertIn("remove worktree", self.sb("gc", at=self.bob, user="bob", agent="codex"))

    def test_yolo_chain_skips_review_until_its_end(self):
        self.plan()
        self.sb("yolo", "SB-2", "on", tty=True)
        self.sb("claim", "SB-1", "--no-worktree", agent="claude-code")
        self.assertIn("skips review", self.sb("finish", "SB-1", "-m", "done", agent="claude-code"))
        self.assertEqual(self.state("SB-1"), "done")
        self.sb("claim", "SB-2", "--no-worktree", agent="claude-code")
        self.sb("finish", "SB-2", "-m", "done", agent="claude-code")
        self.assertEqual(self.state("SB-2"), "review")


class Policies(World):
    config = {"agents": {"max_claims": 1}, "lanes": [{"name": "Platform", "prefix": "PL", "owners": ["alice"], "wip": 1},
                                                    {"name": "Docs", "prefix": "DC"}]}

    def test_wip_and_claim_limits(self):
        self.plan()
        self.sb("propose", "Platform", "Second platform task")
        self.sb("claim", "SB-1", "--no-worktree", agent="claude-code")
        self.assertIn("WIP limit", self.sb("claim", "SB-4", "--no-worktree", at=self.bob, user="bob", agent="codex", code=1))
        self.assertIn("limit per agent is 1", self.sb("claim", "SB-3", "--no-worktree", agent="claude-code", code=1))
        self.sb("claim", "SB-3", "--no-worktree", at=self.bob, user="bob", agent="codex")

    def test_blocked_and_maps_cannot_be_claimed(self):
        self.plan()
        self.assertIn("blocked by unfinished SB-1", self.sb("claim", "SB-2", agent="claude-code", code=1))
        self.sb("map", "new", "Payments", "--destination", "A payments spec")
        self.assertIn("is a map", self.sb("claim", "SB-4", agent="claude-code", code=1))

    def test_declared_paths_warn_about_overlap(self):
        self.plan()
        self.sb("claim", "SB-1", "--no-worktree", agent="claude-code")
        out = self.sb("claim", "SB-3", "--no-worktree", "--paths", "src/api/login.py", at=self.bob, user="bob", agent="codex")
        self.assertIn("overlap work in progress", out)
        self.assertIn("declared paths overlap", self.sb("radar", agent="claude-code", code=1))


class Maps(World):
    def test_wayfinder_map_from_destination_to_decisions(self):
        out = self.sb("map", "new", "Payments rewrite", "--destination", "A spec finance agrees to", "--notes", "Use the glossary",
                      agent="claude-code")
        self.assertIn("proposed: a person agrees the destination first", out)
        self.sb("approve", "SB-1", tty=True)
        self.sb("fog", "SB-1", "--add", "How refunds work across currencies", agent="claude-code")
        self.sb("propose", "-", "Which Stripe API version?", "--map", "SB-1", "--kind", "research", agent="claude-code")
        self.sb("graduate", "SB-1", "refunds", "How do refunds work?", "--kind", "grilling", agent="claude-code")
        self.sb("propose", "-", "Rewrite the ledger", "--map", "SB-1", "--kind", "task", agent="claude-code")
        self.sb("approve", "SB-2", "SB-3", tty=True)
        board = self.board()
        self.assertEqual(K.task_map(board)["SB-2"]["group"], "M1")
        self.assertEqual(S.bullets(S.section(K.task_map(board)["SB-1"]["desc"], "Not yet specified")), [])
        self.assertIn("SB-2", self.sb("frontier", "SB-1", agent="claude-code"))

        self.assertIn("resolved with a person, live", self.sb("claim", "SB-3", agent="claude-code", code=1))
        self.sb("claim", "SB-3", "--with", "alice", "--no-worktree", agent="claude-code")
        self.assertEqual(K.task_map(self.board())["SB-3"]["sb"]["lock"]["pair"], "alice")

        self.sb("claim", "SB-2", "--no-worktree", agent="claude-code")
        self.sb("resolve", "SB-2", "-m", "Use the 2026-09 API: it has payment intents", agent="claude-code")
        self.sb("accept", "SB-2", tty=True)
        decisions = S.section(K.task_map(self.board())["SB-1"]["desc"], "Decisions so far")
        self.assertIn("[Which Stripe API version?]", decisions)
        self.assertIn("Use the 2026-09 API", decisions)

        self.sb("scope-out", "SB-4", "-m", "The ledger is another effort", agent="claude-code")
        self.assertIn("Rewrite the ledger", S.section(K.task_map(self.board())["SB-1"]["desc"], "Out of scope"))
        self.assertNotIn("SB-4", K.task_map(self.board()))


class BrowserSaves(World):
    def apply(self, edit, user="alice"):
        hub = self.hub(user=user, tty=True)
        old = hub.write_mirror()
        new = json.loads(json.dumps(old))
        edit(new)
        hub.invalidate()
        return hub.apply_board(K.parse_board(K.dumps(old)), K.parse_board(K.dumps(new)))

    def test_browser_changes_become_hub_operations(self):
        self.sb("propose", "Platform", "Proposal", agent="claude-code")
        self.sb("propose", "Docs", "Doomed", agent="claude-code")

        def edit(b):
            t = {x["id"]: x for x in b["tasks"]}
            t["SB-1"]["state"] = "todo"
            t["SB-1"]["title"] = "Proposal, approved"
            t["SB-1"].setdefault("notes", []).append({"at": "2026-10-28T10:00:00Z", "by": "human", "text": "Go"})
            b["tasks"].remove(t["SB-2"])
            b["tasks"].append({"id": "PL-01", "title": "From the browser", "state": "todo", "group": "PL", "deps": ["SB-1"],
                               "desc": "", "notes": []})
        done = self.apply(edit)
        self.assertIn("created SB-3", done)
        board = K.task_map(self.board())
        self.assertEqual((board["SB-1"]["state"], board["SB-1"]["title"]), ("todo", "Proposal, approved"))
        self.assertEqual(board["SB-1"]["notes"][-1]["by"], "@alice")
        self.assertNotIn("SB-2", board)
        self.assertEqual((board["SB-3"]["deps"], board["SB-3"]["sb"]["lane"]), (["SB-1"], "Platform"))

    def test_browser_follows_the_same_rules(self):
        self.plan()

        def start(b):
            next(x for x in b["tasks"] if x["id"] == "SB-3")["state"] = "doing"
        with self.assertRaisesRegex(S.SignalboxError, "by claiming it"):
            self.apply(start)
        self.sb("propose", "Platform", "Needs an owner", agent="claude-code")

        def approve(b):
            next(x for x in b["tasks"] if x["id"] == "SB-4")["state"] = "todo"
        with self.assertRaisesRegex(S.SignalboxError, "cannot approve"):
            self.apply(approve, user="bob")
        self.apply(approve, user="alice")
        self.assertEqual(self.state("SB-4"), "todo")

    def test_edited_notes_keep_their_author_and_deletions_need_an_approver(self):
        self.plan()
        self.sb("note", "SB-3", "first take", agent="claude-code")

        def edit(b):
            t = next(x for x in b["tasks"] if x["id"] == "SB-3")
            t["notes"][-1]["text"] = "first take, fixed"
        self.apply(edit)
        note = K.task_map(self.board())["SB-3"]["notes"][-1]
        self.assertEqual((note["by"], note["text"]), ("claude-code", "first take, fixed"))

        def delete(b):
            b["tasks"] = [x for x in b["tasks"] if x["id"] != "SB-1"]
        with self.assertRaisesRegex(S.SignalboxError, "cannot delete"):
            self.apply(delete, user="bob")

    def test_mirror_is_a_valid_kanvas_board(self):
        self.plan()
        self.sb("claim", "SB-1", "--no-worktree", agent="claude-code")
        hub = self.hub()
        board = hub.write_mirror()
        text = K.read_text_file(hub.mirror_path())
        self.assertEqual(K.dumps(K.parse_board(text)), text)
        self.assertEqual(K.problems(K.parse_board(text))[0], [])
        self.assertEqual(board["signalbox"]["hub"], f"file:{K.rel(self.hub_path)}")
        self.assertEqual(K.board_name(hub.mirror_path()), "alice")


class Guard(World):
    config = {"approvers": ["alice"], "agents": {"logins": ["acme-bot"]}}

    def event(self, action, number, sender, label=None, reason=None):
        hub = self.hub()
        issue = hub.tracker.get_issue(number)
        raw = {"number": number, "id": issue["id"], "title": issue["title"], "body": issue["body"],
               "labels": [{"name": x} for x in issue["labels"]], "state": issue["state"], "state_reason": reason,
               "assignees": [], "user": {"login": "alice"}}
        if label:
            raw["labels"].append({"name": label})
        ev = {"action": action, "issue": raw, "sender": {"login": sender}}
        if label:
            ev["label"] = {"name": label}
        return S.guard_event(hub, ev)

    def test_guard_undoes_decisions_by_agents_and_non_approvers(self):
        self.sb("propose", "Docs", "Proposal", agent="claude-code")
        hub = self.hub()
        hub.tracker.add_labels(1, ["sb:todo"])  # the bot approved it on GitHub directly
        self.assertIn("undid approve by @acme-bot", self.event("labeled", 1, "acme-bot", "sb:todo"))
        self.assertEqual(self.state("SB-1"), "proposed")
        self.assertIsNone(self.event("labeled", 1, "alice", "sb:todo"))
        self.sb("approve", "SB-1", tty=True)
        self.sb("claim", "SB-1", "--no-worktree", agent="claude-code")
        self.sb("finish", "SB-1", "-m", "done", agent="claude-code")
        hub.tracker.update_issue(1, state="closed", state_reason="completed")
        self.assertIn("undid mark done by @carol", self.event("closed", 1, "carol", reason="completed"))
        self.assertEqual(self.state("SB-1"), "review")


class FakeGitHub:
    """Just enough of the REST API to check what GitHubTracker sends."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, body=None):
        self.calls.append((method, url.replace("https://api.github.com", ""), body))
        path = url.replace("https://api.github.com", "")
        if path.startswith("/repos/o/r/issues?") and "page=2" not in path:
            return 200, [{"number": 1, "id": 11, "title": "A", "labels": [{"name": "signalbox"}], "state": "open"},
                         {"number": 2, "id": 12, "title": "PR", "labels": [], "state": "open", "pull_request": {}}], \
                {"Link": '<https://api.github.com/repos/o/r/issues?state=all&labels=signalbox&per_page=100&page=2>; rel="next"'}
        if "page=2" in path:
            return 200, [{"number": 3, "id": 13, "title": "C", "labels": ["signalbox"], "state": "closed",
                          "state_reason": "completed", "assignees": [{"login": "Alice"}]}], {}
        if method == "GET" and path.startswith("/repos/o/r/issues/"):
            n = int(path.rsplit("/", 1)[1])
            return 200, {"number": n, "id": 10 + n, "title": "x", "labels": [], "state": "open"}, {}
        if method == "POST" and path == "/repos/o/r/issues":
            return 201, {"number": 9, "id": 19, "title": body["title"], "body": body["body"],
                         "labels": [{"name": x} for x in body["labels"]], "state": "open"}, {}
        if path == "/user":
            return 200, {"login": "Alice"}, {}
        if path.startswith("/repos/o/r/labels") and method == "POST":
            return 422, {"message": "already_exists"}, {}
        return 200, {}, {}


class GitHubTrackerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.gh = S.GitHubTracker("o/r", token="t", transport=self.api)

    def test_lists_issues_across_pages_without_pull_requests(self):
        issues = self.gh.list_issues()
        self.assertEqual([i["number"] for i in issues], [1, 3])
        self.assertEqual((issues[1]["state_reason"], issues[1]["assignees"]), ("completed", ["alice"]))
        self.assertIn("labels=signalbox", self.api.calls[0][1])

    def test_writes_go_to_the_right_endpoints(self):
        issue = self.gh.create_issue("T", "B", ["signalbox", "sb:proposed"])
        self.assertEqual((issue["number"], issue["labels"]), (9, ["signalbox", "sb:proposed"]))
        self.gh.add_labels(9, ["sb:todo"])
        self.gh.remove_label(9, "lane:Platform Team")
        self.gh.ensure_label("sb:todo", "fff", "x")  # 422: exists, fine
        self.assertTrue(self.gh.link_blocked_by(9, 3))
        self.assertTrue(self.gh.link_sub_issue(1, 9))
        sent = [(m, p) for m, p, _ in self.api.calls]
        self.assertIn(("POST", "/repos/o/r/issues/9/labels"), sent)
        self.assertIn(("DELETE", "/repos/o/r/issues/9/labels/lane%3APlatform%20Team"), sent)
        self.assertIn(("POST", "/repos/o/r/issues/9/dependencies/blocked_by"), sent)
        self.assertIn(("POST", "/repos/o/r/issues/1/sub_issues"), sent)
        blocked = next(b for m, p, b in self.api.calls if p.endswith("/dependencies/blocked_by"))
        self.assertEqual(blocked, {"issue_id": 13})
        self.assertEqual(self.gh.whoami(), "alice")

    def test_refusals_explain_themselves(self):
        gh = S.GitHubTracker("o/r", token="", transport=lambda m, u, b=None: (401, {"message": "Bad credentials"}, {}))
        with self.assertRaisesRegex(S.SignalboxError, "401 Bad credentials. Set GH_TOKEN"):
            gh.list_comments(1)


class InitTests(unittest.TestCase):
    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_init_sets_up_a_project_without_touching_existing_files(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="signalbox-init-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sh("git", "init", "--quiet", tmp)
        with open(os.path.join(tmp, "AGENTS.md"), "w") as f:
            f.write("# Ours\n")
        env = dict(CLEAN_ENV, SIGNALBOX_USER="alice", SIGNALBOX_TEST_TTY="0")
        p = subprocess.run([sys.executable, SCRIPT, "init", "--hub", "file", "--prefix", "AC"], cwd=tmp, env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Add this line to AGENTS.md", p.stdout)
        self.assertEqual(read(os.path.join(tmp, "AGENTS.md")), "# Ours\n")
        for rel in (".signalbox/config.json", ".signalbox/.gitignore", ".agents/skills/signalbox/signalbox.py",
                    ".agents/skills/signalbox/kanvas.py", ".agents/skills/signalbox/kanvas.html",
                    ".agents/skills/signalbox/SKILL.md", ".claude/skills/signalbox/SKILL.md"):
            self.assertTrue(os.path.isfile(os.path.join(tmp, rel)), rel)
        cfg = json.loads(read(os.path.join(tmp, ".signalbox", "config.json")))
        self.assertEqual((cfg["prefix"], cfg["hub"]["type"]), ("AC", "file"))
        # The agents' copy runs on its own.
        p = subprocess.run([sys.executable, ".agents/skills/signalbox/signalbox.py", "propose", "-", "First"], cwd=tmp,
                           env=dict(env, SIGNALBOX_AGENT="codex"), capture_output=True, text=True)
        self.assertIn("Proposed AC-1", p.stdout, p.stderr)
        self.assertEqual(sh("git", "status", "--porcelain", "--", ".signalbox", cwd=tmp).split(), ["??", ".signalbox/"])


class HtmlTests(unittest.TestCase):
    def test_page_knows_signalbox_boards(self):
        html = K.read_text_file(K.HTML_FILE)
        for needle in ("const isHuman", "function sbSection", "function lockTag", "signalbox claim"):
            self.assertIn(needle, html)
        self.assertNotIn('!== "human"', html)


if __name__ == "__main__":
    unittest.main()
