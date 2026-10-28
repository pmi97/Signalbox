# Working on Signalbox

Signalbox is a fork of Kanvas: the Kanvas engine (`kanvas.py`, `kanvas.html`) plus `signalbox.py`, which moves the board onto GitHub Issues, guards claims with git-ref interlocks and worktrees, and adds Wayfinder maps. This file is for agents and contributors changing this repository. The instructions Signalbox gives to agents in other projects live in `skill/signalbox/SKILL.md` (and Kanvas's own in `skill/SKILL.md`). Read [DESIGN.md](DESIGN.md) before changing how locks, the hub mapping or the browser saves work.

## Signalbox layout

| Path | What it is |
|------|------------|
| `signalbox.py` | Trackers (GitHub REST, file), the issue<->task mapping, interlocks, worktrees, policies, maps, the CLI and the server subclass. Python 3.8+, standard library only |
| `skill/signalbox/SKILL.md` | Agent skill that `signalbox init` installs into projects |
| `templates/signalbox-guard.yml` | The GitHub Actions workflow `init` installs to undo human-only decisions made by the wrong account |
| `scripts/demo.py` | Builds a local multi-person, multi-agent demo world (bare remote, two clones, file hub) |
| `tests/test_signalbox.py` | Signalbox tests: real git repos, racing clones, a fake GitHub API |
| `DESIGN.md` | Decisions, guarantees and limits |

## Signalbox rules

- **Keep the engine mergeable.** Prefer changing `signalbox.py` over `kanvas.py`/`kanvas.html`; when the engine must change, keep it a small hook that plain Kanvas boards do not notice, so upstream changes still merge (`git fetch upstream && git merge upstream/main`).
- **The ref is the authority.** Every claim goes through `Interlock.acquire`/`replace`/`release` (compare-and-swap); the issue only mirrors it. Never decide ownership from labels or assignees.
- **One rulebook.** CLI commands, browser saves (`Hub.apply_board`) and the guard go through the same `Hub` methods and checks.
- **People are `@login`, agents are bare names.** `is_human()` in both engine files recognises both `human` and `@...`.

# Working on the Kanvas engine

## Layout

| Path | What it is |
|------|------------|
| `kanvas.py` | CLI, board model, local server, `init` / `import` / `export`. Python 3.8+, standard library only |
| `kanvas.html` | The whole browser board in one file: CSS, markup and JavaScript, no build step |
| `skill/SKILL.md` | Agent skill that `init` installs into projects |
| `examples/sample.json` | Demo board, also used by the tests |
| `examples/drone.json` | The board in the README, its screenshots and the skill's examples |
| `tests/test_kanvas.py` | Test suite (`unittest`) |
| `tests/layout_stats.py`, `tests/fixtures/` | Graph clutter measurements and the boards they run on |
| `pyproject.toml` | The `kanvas-board` package, which installs the `kanvas` command. Its version comes from `VERSION` in `kanvas.py`, and the `VERSION` file must match. hatchling and hatch-fancy-pypi-readme, which points the README's relative links at GitHub for PyPI, are needed only to build it |
| `tests/check_installed.py` | Runs an installed `kanvas` command through `init`, subfolders, `export`, `serve` (with a second project joining it) and the project copy |

## Rules

- **Zero dependencies.** `kanvas.py` uses only the standard library and must run on Python 3.8 on Linux, macOS and Windows. `kanvas.html` loads nothing from the network.
- **One format, two writers.** `kanvas.py` (`canonical`, `dumps`) and `kanvas.html` (`canonical`, `serialize`) must produce byte-identical files. Change both together; `test_html_serializer_matches_python` checks it.
- **The CLI enforces the agent workflow.** Agents can propose, start, finish (with a summary), pause, note and add dependencies, and they can edit, withdraw and rewire proposals until a human approves them. Approving, changing approved tasks and marking done stay human-only, in the browser. Turning YOLO on approves the chain's proposals; a proposal an agent links into the chain later stays a proposal.
- **Writes are safe.** Every CLI write goes through `mutate()`: lock, load, change, atomic replace. Failed commands leave the file untouched. Browser saves are compare-and-swap through the server (`If-Match`, 409 with the current board, the change is replayed).
- **Never edit a user's existing files.** `init` only creates files that do not exist yet, apart from the skill copies it owns.

## Testing

```bash
python -m unittest discover -s tests -v
```

`tests/test_browser.py` runs `kanvas.html` in headless Chrome against `kanvas.py serve` and checks loading, the task panel, saving notes, failed saves and live updates from the CLI. It skips where it finds no Chrome or Chromium; `KANVAS_TEST_CHROME` names the browser to use, and the Linux CI jobs set it so the test runs there.

When changing `pyproject.toml`, or how `kanvas.py` finds `kanvas.html` and `SKILL.md`, build and install the package and check the installed command, as CI does on Linux, macOS and Windows:

```bash
uv build && uv tool install --force dist/*.whl
python tests/check_installed.py
```

The graph layout lives in the "Graph layout" section of `kanvas.html`, which must stay free of DOM and app state so it runs under Node. When changing it, compare the clutter before and after:

```bash
python tests/layout_stats.py -v          # crossings, arrows under cards, straight arrows and bends, per board and flow
python tests/layout_stats.py --manual    # the same for the manual layout, where cards keep their file order
python tests/layout_stats.py --unfolded  # the same with every lane's finished tasks shown, as when all their stubs are open
```

`LayoutTests` fails if crossings, arrows under cards or bends exceed its ceilings; lower them when the layout improves. In the browser console, `kanvasLayoutStats()` measures the board on screen.

For changes to `kanvas.html`, also check the page in a browser:

```bash
cp examples/sample.json /tmp/demo/kanvas.json
cd /tmp/demo && python /path/to/kanvas.py serve
```

Then exercise both views, the task panel, and live updates by running CLI commands (`start`, `note`, `finish`) in another terminal while the page is open. Opening `kanvas.html` directly (file mode) and `kanvas.py export` (read-only snapshot) are separate code paths worth a look when touching storage or rendering.

## Releasing

Set the version in `kanvas.py` (`VERSION = "..."`) and in the `VERSION` file, then publish a GitHub release tagged `v` plus that version. `.github/workflows/release.yml` checks the tag, builds once, runs `tests/check_installed.py` on Linux, macOS and Windows, and uploads to PyPI through trusted publishing in the `pypi` environment. Test versions such as `0.4.0rc1` need a release marked as a pre-release, and final versions need one that is not; the workflow refuses a mismatch. Pull requests that change the workflow run it without the upload.

## Cursor Cloud specific instructions

- Python 3 and Node.js are preinstalled; no setup step is needed.
- Run long-lived `kanvas.py serve` processes in tmux with `--no-open`, and use `--port` to keep test boards out of a Kanvas that is already running: `serve` opens its board in any Kanvas it finds on that port or the 19 after it.
- Headless Chrome (`google-chrome --headless=new`) works for screenshots of `http://127.0.0.1:<port>/<folder>/`.
