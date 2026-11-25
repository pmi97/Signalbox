# Signalbox design

This note records why Signalbox is built the way it is, what it guarantees, and where those guarantees end.

## The problem

Kanvas made a board that one person shares with their agents: a JSON file in the repository, a CLI that enforces the workflow, a live page in the browser. Wayfinder made a way to plan efforts too foggy for one session: a map of decision tickets on the issue tracker, resolved one at a time. Neither was built for what teams now do daily: **several groups of people, each with several agents, working on one codebase at the same time, on different machines.** That needs four things neither had:

1. **One shared source of truth** that every person and agent can reach, with identity, history and permissions.
2. **Mutual exclusion that holds across machines**: two agents must never both believe they own a task.
3. **Isolation of the work itself**: each task in its own checkout, so agents don't trample each other's files.
4. **Clear authority**: who may approve, accept or override, enforced rather than requested.

## Decisions

### The hub is GitHub Issues, mapped losslessly

Issues already hold identity (who did what), permissions (who may write), history, notifications and a UI everyone has. Signalbox maps the board onto them so a person on github.com, an agent in the CLI, and the browser board all see the same thing:

| Board | Issue |
|---|---|
| task | issue labelled `signalbox` |
| state | `sb:proposed` / `sb:todo` / `sb:doing` / `sb:review`; done = closed as completed; rejected or withdrawn = closed as not planned |
| lane (Kanvas group) | label `lane:<name>`; prefix, owners and WIP limit in the label description or the config |
| kind | label `kind:research|prototype|grilling|task|map` (build has none) |
| dependencies, declared paths, proposer | a hidden `<!-- signalbox:meta {...} -->` block at the end of the body, mirrored to GitHub's native "blocked by" |
| map and its tickets | an issue with `kind:map` and Wayfinder's sections; tickets are native sub-issues and carry `map` in their meta |
| notes | comments, each with a hidden header naming the author (`claude-code`) and sponsor (`alice`) |
| YOLO, lock mirror | labels `sb:yolo`, `sb:locked`, plus the holder's sponsor as assignee |

The metadata block is the source of truth for dependencies because GitHub's dependency API is new and not everywhere. The native links are a best-effort mirror that shows the frontier in GitHub's own UI, as Wayfinder recommends.

People are written `@login` and agents by tool name, so the Kanvas page (patched to treat both `human` and `@…` as people) shows who did what across a team.

### The interlock is a git ref, changed only by compare-and-swap

Issue assignees are not a lock: two clients can assign themselves at the same moment and both succeed. Signalbox needs a primitive that is **atomic, shared by every machine, and available wherever the code is**. Git already has one: a push with `--force-with-lease=<ref>:<expected>` updates a ref only if it still has the expected value, and an empty expected value means "only if it does not exist". So:

- **claim**: create `refs/signalbox/locks/<n>`, or move it on from a "free" record, to a commit whose message is the lock record. Exactly one racing push wins; the loser reads the winner's record and says who holds it (exit code 3).
- **heartbeat / take over**: add a commit with the new record on top, expecting the sha you read.
- **release**: add a "free" record on top, expecting the sha you hold.

The ref is **append-only**: every move is a fast-forward and nothing is ever deleted or force-pushed. The first version deleted refs on release; the trial on a real GitHub repository went through a git proxy that, like many protected-branch setups, refuses deletions and force-pushes, so release could not work there. Lock records live in empty-tree commits, so a chain costs a few bytes per move. Remotes that accept nothing but branches (that proxy again) take `"locks": {"namespace": "refs/heads/sb-lock"}`: the locks then show as `sb-lock/<n>` branches, and an error names this option when a push to `refs/signalbox/*` is refused. Without a remote, the same operations use `git update-ref <ref> <new> <old>` on the repository's own refs, which all its worktrees share, so agents on one machine are still serialized. `tests/test_signalbox.py` races two clones through a real remote repeatedly and requires exactly one winner each time.

**Leases** answer crashed agents: a record carries `expires`, `heartbeat` pushes it forward, and once it has passed anyone may `claim --take-over` (a compare-and-swap from the stale sha, so two rescuers cannot both win). A *live* lock can only be broken by a person at a terminal (`unlock`), and the break is written on the issue.

The issue is the **visible mirror** of the lock (state `doing`, `sb:locked`, assignee, a 🔒 comment), and the ref is the **authority**. `status` and `doctor` report any disagreement: work in doing without a lock, or a lock on a task that is not in doing. `gc` repairs the second.

### Worktrees, and what Signalbox adds to them

`git worktree` gives isolation and nothing else. The rest of what a team needs around it is Signalbox's job:

| Missing from plain worktrees | Signalbox |
|---|---|
| Others can't see who works where | the lock record names holder, host, branch and worktree; the board shows it |
| Nothing stops two worktrees for one task on two machines | the interlock is taken before the worktree is created |
| `git worktree prune` can remove a live one | `git worktree lock --reason "signalbox: SB-12 held by …"` |
| A new worktree lacks `.env`, dependencies, a free port | `worktrees.copy`, `worktrees.setup`, `SB_PORT` = `port_base + issue % 1000` |
| The agent in it doesn't know what it is for | `.signalbox/TASK.md`: task, notes, map destination and decisions, the commands to use |
| Work is stranded on one machine | `finish`, `pause` and `handoff` push the branch; `claim` on any machine resumes it |
| Overlapping changes surface only at merge | declared `--paths` warn (or block) at claim; `radar` compares branches and uncommitted changes |
| Old worktrees and branches pile up | `gc` removes worktrees of done tasks and those left behind by a handoff, and deletes merged branches |

### Authority: two layers

- **The CLI** refuses moves that are not the actor's: agents cannot approve, accept, send back, reject, turn YOLO on or break live locks. Agents can edit only proposals, never claim blocked tasks, and only the holder can finish. Human decisions also need a person at the terminal (stdin and stdout are TTYs), because an agent running a shell without signing would otherwise pass as its sponsor; for the same reason a person who is not at a terminal, or not an approver of the lane, proposes rather than adds approved work. In the browser, the server acts as the logged-in person and replays every edit through the same checks.
- **Identity comes from the token.** On a GitHub hub, `whoami` asks GitHub; `SIGNALBOX_USER` counts only where GitHub cannot answer (a workflow token), and then the identity is *unverified* and makes no human decisions. An agent's sponsor is the token's owner whatever `SIGNALBOX_SPONSOR` says, unless the token belongs to an agent account in `agents.logins`, which is never treated as a person. Locks are told apart by agent and sponsor, so two people's `claude-code` never share one.
- **The guard workflow** runs on GitHub for every labelled, closed or edited `signalbox` issue. GitHub records who made each change, so the guard can undo, with a comment saying why: an approval (however it is labelled, including an issue made approved from the start), a send-back, an acceptance, a rejection of approved work, YOLO, and an agent account's rewrite of an approved task's title, description or dependencies (on maps, of the destination and notes), when made by an account in `agents.logins` or by someone outside the lane's approvers. Bots get no exemption. This catches clients that never run Signalbox at all, such as a script or a click on github.com. An open issue with no state label reads as a proposal, so removing labels approves nothing either.

**Limit:** if an agent uses its sponsor's own token, GitHub cannot tell them apart, and the guard can't either. The CLI's checks still apply, but an agent determined to bypass them could call the API directly, or fake a terminal. Give agents their own bot account or GitHub App for real enforcement.

### Trust boundaries

Signalbox reads from places other people write to, and it holds a token. Each boundary is drawn where the data comes from:

| Input | Who can write it | How Signalbox treats it |
|---|---|---|
| `.signalbox/config.json` | anyone who gets a commit merged; any branch | read from the **main worktree** only, never from the task branch an agent works on, so a branch cannot change approvers, the hub, the setup command or the checks before review |
| `hub.api` in the config | as above | the token goes only to `https://api.github.com`, the host of `origin` (GitHub Enterprise), or a host in `SIGNALBOX_GITHUB_HOSTS` on that machine; pagination links are followed only on the same origin |
| Lock records (`refs/signalbox/locks/*`) | anyone with push access | data: branch names must look like `sb/...` and pass git's rules, worktree paths must be linked worktrees of this repository, before either reaches a git command (a branch named `--output=...` would otherwise be an option) |
| Issue and comment text | on a public repository, anyone | comments and issues by authors who are not owners, members or collaborators (GitHub's `author_association`) are marked *from outside the team* in briefs, `show`, JSON and MCP, quoted in `TASK.md`, and can never write a map's decisions; a signature claiming a person counts only when that person's account posted it |
| Lane label descriptions | anyone who edits labels | display only: lane owners, who approve, come from the config; `add-lane --owners` writes them there for the team to commit |
| `worktrees.copy` | the config | patterns must stay inside the repository (no absolute paths, no `..`) |
| The `serve` port | any process on the machine | refused without the session's key, delivered once in the link `serve` opens and then kept in an HttpOnly cookie; a Signalbox server opens no other boards and nothing to phones |
| MCP clients | the agent | `signalbox mcp` always acts as an agent and has no human-decision tools |

The guard reads its rules from the default branch, keeps no credentials in the checkout, and runs one job per issue at a time.

**Limits.** An agent with its sponsor's token and a pseudo-terminal can still pass as its person at the CLI; only separate agent accounts make that visible to the guard. Changes made with a workflow's `GITHUB_TOKEN` do not trigger workflows, so the guard does not see changes other workflows make. `worktrees.setup` and `checks` are shell commands from the reviewed config and run with your rights, as CI would; review changes to them like code.

### Wayfinder on the same board

A map is an issue of kind `map` with Wayfinder's five sections, and appears as a swimlane. Its tickets are ordinary tasks of a decision kind, so they get the same interlocks, review and dependencies. Signalbox enforces what Wayfinder could only ask for:

- the destination is agreed first: an agent's `map new` is a proposal a person approves;
- **HITL** tickets (`grilling`, `prototype`) can only be claimed by an agent `--with` its person, which answers Wayfinder's reported failure of agents answering their own grilling questions;
- maps can't be claimed or built; `resolve` records an answer, and accepting it appends the gist to *Decisions so far* automatically, so the map stays an index and never a store;
- `fog`, `graduate` and `scope-out` keep *Not yet specified* and *Out of scope* honest: graduated fog leaves the fog, and out-of-scope tickets are closed.

### The browser board is Kanvas, fed by a mirror

`serve` builds a Kanvas-format board from the hub into `.signalbox/board.json`, extra data under `sb`, and serves Kanvas's page through a subclass of its server. It refreshes the mirror every `poll_seconds`. When the page saves, the server diffs the saved board against the mirror and replays the difference as hub operations: create, close, retitle, re-lane, dependencies, notes and state moves, each through the same rules. Then it rebuilds the mirror and answers. A refused move comes back as an error the page shows, and the page reloads the truth. This keeps Kanvas's UI (graph layout, Kanban, undo, keyboard) nearly unchanged, so it stays mergeable with upstream. The `kanvas.py` changes are three small hooks: a pluggable handler class, the board name for `.signalbox/`, and `@login` people.

## The factory layer

Agents in quantity need a dispatcher, structured answers and gates, so Signalbox has them built in rather than bolted on:

- **`next`** ranks claimable tasks (sent back with feedback, then under YOLO, handed to you, unblocking the most, oldest) and skips what the actor may not take (HITL tickets without a person, lanes at their WIP limit, handoffs to others). `next --claim` claims down that list and treats a lost race as a reason to try the next task, never to retry the same one, so any number of workers can run the same loop.
- **`--json`** on every command and the **MCP server** share one data layer, so a script, an orchestrator and an agent in an IDE see the same task shape.
- **Checks** run in the task's own worktree at `finish`: the gate sits where the work is handed over, before review, and its results are part of what the reviewer reads.

## Known limits

- Writes to one issue body (dependencies, map sections) are read-modify-write and can lose a concurrent edit of the same body. Interlocks protect *claims*, not every field. Moves on a task in progress go through its single holder, which keeps this rare.
- The GitHub tracker lists all `signalbox` issues and re-reads comments only for issues whose `updated_at` changed (cached in `.signalbox/cache.json`). Very large hubs (thousands of open tasks) will want webhooks or GraphQL instead of polling.
- Pushing `refs/signalbox/*` requires write access to the repository. Branch-protection rules usually match `refs/heads/*` and leave these alone. Where only branches are accepted, `sb-lock/<n>` branches stay after their tasks are done (released, holding a "free" record); delete them by hand if the remote allows it.
- GitHub's issue listing lags a few seconds behind writes. Within one command Signalbox overlays the issues it just wrote; across separate commands a just-changed label can read stale for those seconds.
- `serve` serves one project per process (Kanvas's multi-board server is kept for plain Kanvas boards).
