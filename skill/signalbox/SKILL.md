---
name: signalbox
description: Plan, claim and track work on the project's Signalbox board, shared through GitHub Issues with several people and other agents. Every task you work on is held by an interlock (a lock in git) and done in its own git worktree. Use when the user mentions the board, Signalbox, tasks, planning, a map, "what's next", or a task ID such as SB-12.
license: MIT
compatibility: Requires Python 3.8 or later and git.
---

# Signalbox

The board lives on the hub, usually this repository's GitHub Issues, and you share it with **several people and other agents** working at the same time, on other machines. Change it only through the CLI in `.agents/skills/signalbox/`:

```bash
python .agents/skills/signalbox/signalbox.py <command>
```

Use `python3` where `python` is missing. Never edit issues, labels or `refs/signalbox/*` by hand, and never edit `.signalbox/board.json`: it is a mirror the CLI rewrites.

**Sign every command** with your tool's name, so people can tell who did what: set `SIGNALBOX_AGENT=claude-code` (or `codex`, `cursor`, `gemini`, `copilot`, `opencode`) once if your shell keeps variables, or pass `--as claude-code` each time. When several agents of one tool share the board, add what each does, as in `claude-code-docs`. Without a signature you act as the person whose login runs the command, which is not what you are.

## The rules of the road

```
proposed -> todo -> doing -> review -> done
```

| Move | Who | How |
|------|-----|-----|
| propose a task, a map, a lane | you | `propose`, `batch`, `map new`, `add-lane` |
| approve a proposal | a person (an approver of its lane) | in the board or their own terminal |
| todo -> doing | whoever wins the **interlock** | `claim` |
| doing -> review | the holder | `finish` (or `resolve` for decision tickets) |
| doing -> todo | the holder | `pause`, `handoff` |
| review -> done, or back to todo | a person | in the board or their own terminal |

You may propose, claim ready tasks, note, ask, finish, pause, hand off, add dependencies, and revise, withdraw or rewire **proposals**. Approving, accepting, sending back, rejecting, YOLO and breaking someone else's lock belong to people: ask for them in the conversation. The CLI refuses those to agents and to any command run without a person at the terminal, and the hub's guard workflow undoes them when they are made on GitHub by the wrong account. Do not look for a way around a refusal; it breaks the team's trust in the board.

## Interlocks and worktrees

`claim ID` does three things, atomically decided:

1. **Takes the interlock**, a lock ref on the shared remote. If two agents claim the same task at once, exactly one wins; the other gets `Locked:` and exit code 3. Pick another task, never retry the same one in a loop.
2. **Moves the task to doing** and records you as holder, for your sponsor (the person you work for).
3. **Creates a worktree** for it, `../<repo>.worktrees/SB-12` on branch `sb/sb-12-<title>`, with `.signalbox/TASK.md` (your brief: the task, its notes, its map) and `.signalbox/env` (`SB_PORT` and friends: use that port for any server you start, so you never collide with another agent).

**Work only inside your task's worktree** (`cd` into the path `claim` prints), commit there, and leave the main checkout and other agents' worktrees alone.

The lock has a **lease** (two hours by default). On long work run `heartbeat ID` every hour or so; a lock whose lease ran out can be taken over by anyone with `claim ID --take-over`, and its branch resumes in their worktree.

## Session protocol

1. **Read the board:** `status`. Tell the user what waits for their review, what is ready, which interlocks are held and by whom, and repeat any question marked `(waiting for the human: ...)`. Revise proposals marked `(changes requested: ...)` with `edit ID ... -m "what changed"`.
2. **Pick a task:** if the user named one, `show ID`. Otherwise `ready` (or `frontier MAP` on a map); when several are ready, ask which to take.
3. **Claim it:** `claim ID -m "Plan: ..." --paths "src/api/**"`. `--paths` declares what you expect to touch; you are warned when it overlaps work in progress. Read the brief and every note. A note starting with "Sent back:" is review feedback you must address first.
4. **Work** in the worktree. Commit as you go. Record findings with `note ID "..."`. Propose extra work you discover with `propose LANE TITLE --deps ID`. Run `radar` before large changes to see whether someone else is changing the same files; if so, note it on both tasks or ask the user.
5. **Finish:** commit everything, then `finish ID -m "..."` with a summary a reviewer can accept or send back from (what changed for them, how you checked it, what to look at). It pushes your branch and releases the lock. Add `--pr` when the user wants a pull request. Then stop and tell the user it is ready for review. Under YOLO, the tasks the YOLO task depends on go straight to done; carry on with the next ready `(YOLO)` task.

To stop halfway: `pause ID -m "where I left off"` (pushes the branch, releases the lock). To pass the work to someone: `handoff ID --to LOGIN -m "context"`. They resume your branch with `claim ID`.

When you need a person's answer before going on: `ask ID "the question"`, ask the same in the conversation, and stop. When they reply, record it with `note ID "..."`.

## Maps: finding the way through fog

Some efforts are too big and too unclear for one session. Chart them as a **map** (Wayfinder): an index issue with a **destination**, a **fog of war** ("Not yet specified") and **decision tickets** under it.

- `map new "Title" --destination "what reaching the end looks like" --notes "skills to consult, standing preferences"` proposes the map; a person agrees the destination before tickets matter.
- Tickets are questions, not slices of a build: `propose - "Which queue fits?" --map SB-4 --kind research` (also `prototype`, `grilling`, `task`). A batch takes `"map": "SB-4"`.
- **HITL kinds (`grilling`, `prototype`) are resolved live with a person.** Claim them only `--with LOGIN` while that person is in the conversation, and never answer your own questions for them. `research` is AFK: do it alone. `task` is manual work that unblocks a decision, never part of the build.
- `frontier SB-4` lists what is takeable now. Resolve one ticket per session (research excepted): `claim`, then `resolve ID -m "the answer"`. Once a person accepts it, the answer joins the map's **Decisions so far** by itself.
- What you can sense but not yet phrase sharply goes in the fog: `fog SB-4 --add "..."`. When a resolution makes it specifiable, `graduate SB-4 "match" "The sharp question" --kind grilling`. Work beyond the destination: `scope-out ID -m "why"`.
- **Plan, don't do.** A map ends when nothing is left to decide; it hands off to a spec and build tasks, it does not build.

## Planning

When asked to plan, learn the work first (code, docs, the board), then propose in one batch, checked in full before anything is created:

```bash
python .agents/skills/signalbox/signalbox.py batch <<'EOF'
{
  "lanes": ["Payments"],
  "tasks": [
    {"title": "Store cards with the payment provider", "lane": "Payments", "desc": "Cards never touch our database; done when checkout works with a test card", "paths": ["src/payments/**"]},
    {"title": "Refunds from the admin page", "lane": "Payments", "deps": ["Store cards with the payment provider"]}
  ]
}
EOF
```

`deps` takes task IDs or titles of earlier tasks in the same batch. Lanes are teams or areas of work: they have owners who approve and accept their tasks, and may have a WIP limit, so `claim` can refuse a full lane. Give each task a `desc` that says what it achieves and what finished means: people approve from it and review against it.

## Writing for people

Several people read the board between other work, without your context. Open with what matters for their decision (what now works, what a proposal would achieve), make what they should check or decide easy to find, name things as they appear in the product, and leave out the journey. A small fix takes a line; a risky change takes the room to lay out its trade-offs. When unsure, write less.

## Commands

| Command | Purpose |
|---------|---------|
| `status` | Board, interlocks, lanes, maps, problems |
| `ready` / `frontier [MAP]` / `blocked` | What can be claimed / a map's takeable edge / what waits |
| `show ID` / `list [STATE, KIND or LANE]` | Details / tasks by lane and map |
| `claim ID [-m PLAN] [--paths GLOB ...] [--with LOGIN] [--take-over]` | Interlock + doing + worktree |
| `heartbeat ID` | Renew your lease |
| `note ID TEXT` / `ask ID QUESTION` | Note / show you wait for a person |
| `finish ID -m SUMMARY [--pr]` / `resolve ID -m ANSWER` | Push, to review (or done under YOLO), release |
| `pause ID -m NOTE` / `handoff ID --to LOGIN -m NOTE` | Push, back to todo, release |
| `propose LANE TITLE [DESC] [--deps ID ...] [--kind K] [--map ID] [--paths GLOB ...]` | Propose (`-` as LANE for none) |
| `batch` / `add-lane NAME` | Many proposals from JSON / a lane |
| `depend ID ON ...` / `undepend ID ON ...` | Add / remove dependencies (removing: proposals only) |
| `edit ID [--title] [--lane] [--kind] [--desc] [-m NOTE]` / `withdraw ID ...` | Revise / withdraw proposals |
| `map new TITLE --destination TEXT` / `fog MAP [--add T] [--remove M]` / `graduate` / `scope-out` | Maps |
| `locks` / `worktrees` / `radar` | Who holds what / task worktrees / overlapping work |
| `whoami` / `doctor` | Who you act as / access and invariants |

Text arguments also accept `--stdin` and `--file PATH` (UTF-8 everywhere); use a heredoc for multi-line summaries.

## When a command fails

It exits non-zero with `Error:` (a rule, explained) or `Locked:` (someone else holds it, exit code 3). Follow the message or ask the user.
