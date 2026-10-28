#!/usr/bin/env python3
"""Build a Signalbox demo world in a folder, with no GitHub needed.

    python scripts/demo.py /path/to/demo
    cd /path/to/demo/alice && SIGNALBOX_USER=alice python ../../signalbox.py serve

The world: a bare repository plays the shared remote, `alice` and `bob` are two people's clones, and a file hub
plays GitHub Issues. Two agents (claude-code for alice, codex for bob) and the two people plan, claim, race, hand off
and review work, so the board shows every state: interlocks held and expired, a question waiting, work in review,
and a Wayfinder map with its frontier and fog.
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SB = [sys.executable, os.path.join(ROOT, "signalbox.py")]


def sh(*args, cwd=None, env=None, check=True, stdin=None):
    p = subprocess.run(args, cwd=cwd, env=env, input=stdin, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit(f"{' '.join(args)} failed:\n{p.stdout}{p.stderr}")
    return p.stdout


def sb(where, *args, user, agent=None, person=False, stdin=None, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SIGNALBOX_", "KANVAS_"))}
    env.update(SIGNALBOX_USER=user, SIGNALBOX_TEST_TTY="1" if person else "0")
    if agent:
        env["SIGNALBOX_AGENT"] = agent
    out = sh(*SB, *args, cwd=where, env=env, stdin=stdin, check=check)
    print(f"  {agent or '@' + user}$ signalbox {' '.join(args)}")
    return out


def commit(path, rel, text, message):
    full = os.path.join(path, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "a", encoding="utf-8") as f:
        f.write(text)
    sh("git", "add", "-A", cwd=path)
    sh("git", "commit", "--quiet", "-m", message, cwd=path)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    world = os.path.abspath(sys.argv[1])
    if os.path.exists(world) and os.listdir(world):
        sys.exit(f"{world} is not empty")
    os.makedirs(world, exist_ok=True)
    remote, alice, bob = (os.path.join(world, x) for x in ("remote.git", "alice", "bob"))
    sh("git", "init", "--quiet", "--bare", remote)
    sh("git", "symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    for who, path in (("alice", alice), ("bob", bob)):
        sh("git", "clone", "--quiet", remote, path, check=False)
        sh("git", "config", "user.email", f"{who}@example.com", cwd=path)
        sh("git", "config", "user.name", who, cwd=path)
    sh("git", "checkout", "--quiet", "-b", "main", cwd=alice)
    commit(alice, "README.md", "# Acme shop\n", "Start")
    print("Setting up Signalbox in alice's clone")
    sb(alice, "init", "--hub", "file", "--prefix", "SB", "--title", "Acme shop", user="alice")
    cfg_path = os.path.join(alice, ".signalbox", "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["hub"]["path"] = os.path.join(world, "hub.json")  # both clones share the hub, as they would share GitHub
    cfg["approvers"] = ["alice"]
    cfg["lanes"] = [{"name": "Platform", "prefix": "PL", "owners": ["alice", "bob"], "wip": 3},
                    {"name": "Payments", "prefix": "PY", "owners": ["bob"]}, {"name": "Docs", "prefix": "DC"}]
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    sh("git", "add", "-A", cwd=alice)
    sh("git", "commit", "--quiet", "-m", "Set up Signalbox", cwd=alice)
    sh("git", "push", "--quiet", "-u", "origin", "main", cwd=alice)
    for path in (alice, bob):
        sh("git", "fetch", "--quiet", cwd=path)
        sh("git", "remote", "set-head", "origin", "main", cwd=path)
    sh("git", "checkout", "--quiet", "-B", "main", "origin/main", cwd=bob)

    print("\nclaude-code plans the work with alice")
    plan = {"tasks": [
        {"title": "Sessions API", "lane": "Platform", "paths": ["src/api/sessions/**"],
         "desc": "Login and logout endpoints with cookie sessions.\n\n- [ ] POST /login\n- [ ] POST /logout"},
        {"title": "Rate-limit login", "lane": "Platform", "deps": ["Sessions API"], "paths": ["src/api/sessions/**"]},
        {"title": "Checkout page", "lane": "Payments", "paths": ["web/checkout/**"]},
        {"title": "Refunds from the admin page", "lane": "Payments", "deps": ["Checkout page"]},
        {"title": "Getting started guide", "lane": "Docs", "paths": ["docs/**"]},
        {"title": "API reference", "lane": "Docs", "deps": ["Sessions API"]},
    ]}
    sb(alice, "batch", user="alice", agent="claude-code", stdin=json.dumps(plan))
    sb(alice, "approve", "SB-1", "SB-2", "SB-3", "SB-5", "SB-6", user="alice", person=True)
    sb(bob, "approve", "SB-4", user="bob", person=True)

    print("\nA Wayfinder map for the fog around payouts")
    sb(alice, "map", "new", "Marketplace payouts", "--destination",
       "A spec for paying sellers weekly, agreed by finance and legal", "--notes", "Consult the finance glossary",
       user="alice", agent="claude-code")
    sb(alice, "approve", "SB-7", user="alice", person=True)
    sb(alice, "propose", "-", "Which payout provider supports our countries?", "--map", "SB-7", "--kind", "research",
       user="alice", agent="claude-code")
    sb(alice, "propose", "-", "Weekly or on-demand payouts?", "--map", "SB-7", "--kind", "grilling",
       "--deps", "SB-8", user="alice", agent="claude-code")
    sb(alice, "fog", "SB-7", "--add", "How disputes claw back paid-out money", user="alice", agent="claude-code")
    sb(alice, "approve", "SB-8", "SB-9", user="alice", person=True)

    print("\nTwo agents race for the same task; the interlock picks one")
    sb(alice, "claim", "SB-1", "-m", "Plan: cookie sessions behind the existing auth middleware", user="alice", agent="claude-code")
    out = sb(bob, "claim", "SB-1", user="bob", agent="codex", check=False)
    print("   ", (out or "codex lost the race").strip().splitlines()[0] if out else "    codex lost the race: SB-1 is locked")
    wt = os.path.join(world, "alice.worktrees", "SB-1")
    commit(wt, "src/api/sessions/login.py", "def login(): ...\n", "Login endpoint")
    sb(alice, "ask", "SB-1", "Should sessions last 24 hours or 30 days with remember-me?", user="alice", agent="claude-code")

    print("\ncodex takes payments, a person takes docs")
    sb(bob, "claim", "SB-3", "--paths", "web/checkout/**", user="bob", agent="codex")
    commit(os.path.join(world, "bob.worktrees", "SB-3"), "web/checkout/page.tsx", "export {}\n", "Checkout page")
    sb(bob, "finish", "SB-3", "-m", "Checkout works with a test card.\n- Try it: `npm run dev`, then /checkout\n"
       "- Cards never touch our database", user="bob", agent="codex")
    sb(bob, "claim", "SB-5", "--no-worktree", user="bob")

    print("\nResearch for the map, resolved without a person")
    sb(alice, "claim", "SB-8", "--no-worktree", user="alice", agent="claude-code-research")
    sb(alice, "resolve", "SB-8", "-m", "Stripe Connect covers all 14 countries; Adyen misses two", user="alice",
       agent="claude-code-research")
    sb(alice, "accept", "SB-8", user="alice", person=True)

    print(f"\nDone. Open the board:\n  cd {alice}\n  SIGNALBOX_USER=alice python {SB[1]} serve")


if __name__ == "__main__":
    main()
