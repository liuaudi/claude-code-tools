# claude-code-tools

Scan your installed **Claude Code** (the CLI binary *and* the desktop app) and report **which of Anthropic's built-in tools are actually bundled in the binaries right now** — then track how that set changes across releases.

It reads what's on disk. No API key, no account, and the core scan makes no network calls. It does **not** ask Claude what tools it has — it greps the compiled binaries directly, so the answer is ground truth, not self-report.

```text
| Tool group                                         | CLI binary | Desktop (bundle + embedded CLI) |
|----------------------------------------------------|------------|---------------------------------|
| File: Read, Write, Edit, MultiEdit, NotebookEdit…  | ✅ all      | ✅ all                           |
| Shell: Bash, PowerShell                            | ✅ all      | ✅ all                           |
| Web: WebFetch, WebSearch                           | ✅ all      | ✅ all                           |
| Subagent: Agent, Task                              | ✅ all      | ✅ all                           |
| Cron: CronCreate, CronDelete, CronList             | ✅ all      | ✅ all                           |
| …                                                  |            |                                 |

_CLI 2.1.146_ · _Desktop 1.8555.2 (embedded CLI 2.1.149)_ · _captured 2026-06-01_
```

## What it's for

- **"What tools does my Claude Code actually have?"** — answered from the bytes, not from a model's claims.
- **Compare CLI vs Desktop.** The two surfaces ship different tool sets, and the desktop app bundles its *own* separately-versioned CLI inside itself. This shows the breakdown.
- **Track changes across releases.** Save a snapshot per version; the next run diffs against it and tells you exactly which tools were added or removed.

## Requirements

- **macOS.** The paths (`~/.local/share/claude`, `/Applications/Claude.app`, the LaunchAgent, `strings`, `open`) are mac-specific.
- **Python 3.8+** — standard library only, nothing to `pip install`.
- **`strings`** — ships with the Xcode Command Line Tools (already present on most dev machines; `xcode-select --install` if not).
- **Node / `npx`** *(optional)* — only the desktop app's Electron-bundle scan uses `npx @electron/asar` to unpack `app.asar` (downloaded on first use, then cached). Without Node, the CLI scan and the desktop *embedded-CLI* scan still work; only the Electron-JS half of the desktop surface is skipped.

## Quick start

```bash
git clone https://github.com/liuaudi/claude-code-tools.git
cd claude-code-tools

# A readable grouped table of CLI vs Desktop:
python3 scripts/inspect.py --table

# Or the full machine-readable snapshot (JSON):
python3 scripts/inspect.py
```

That's it — no setup, no key. The first run works on a clean clone because the skill ships a **baseline list of known tool names** to check the binaries against. As you keep running it across Claude Code updates, it accumulates its own snapshot history and the diff gets richer.

### How to read the JSON

Per surface (`cli`, `desktop`) the key fields are:

- **`present`** — tool names found in that binary.
- **`missing_vs_expected`** — names we expected (from the baseline, your snapshot history, or live taps) but did **not** find. Usually means a tool was removed in this release.
- **`present_by_artifact`** (desktop only) — which tools came from the Electron bundle vs the embedded CLI.
- **`expected_provenance`** — where each expected name came from: `from_baseline`, `from_snapshots`, `from_live_taps`.
- **`_diff`** — added/removed since your most recent saved snapshot.

By default every run **saves a snapshot** to `snapshots/` *only if something changed*, and **diffs** against the previous one. Opt out with `--no-save` / `--no-diff`; force a write with `--force-save`.

## Optional: pair with monitor-claude-code

This tool confirms what's *bundled on disk*. Its companion, [**monitor-claude-code**](https://github.com/liuaudi/monitor-claude-code), captures what Claude Code actually *sends to the API* — the live tool registry. Install both and they reinforce each other:

- **Live discovery** — a brand-new tool shows up in `from_live_taps` the moment it appears in an API request, before it's in any snapshot.
- **Full reference** — monitor-claude-code's exporter produces the complete system prompt + every tool's description and parameter schema. With it installed, `--open` launches that reference and the scan reports when it's stale versus your current binary.

Neither is required. With no companion installed, the lines below just report cleanly that the optional pieces aren't there. Point this tool at a companion in a non-default location with:

```bash
export CLAUDE_CODE_TOOLS_TAPS=/path/to/monitor-claude-code/taps
export CLAUDE_CODE_TOOLS_REFERENCE=/path/to/_reference
```

## Optional: auto-snapshot on every Claude Code update

A launchd watcher can run the scan automatically whenever the CLI or desktop binaries change on disk, so you never have to remember to capture a release:

```bash
bash scripts/install-watcher.sh     # install (idempotent)
bash scripts/uninstall-watcher.sh   # remove
```

It writes a snapshot (only if the tool set changed) and shows a macOS notification on real changes. Logs land in `~/Library/Logs/claude-code-tools-*`.

## Using it as a Claude Code skill

This repo is also a Claude Code skill (the `SKILL.md` at its root). To let Claude Code run it for you, symlink the cloned folder into your skills directory:

```bash
ln -s "$(pwd)" ~/.claude/skills/claude-code-tools
```

Restart Claude Code so it picks up the skill. You can then ask things like *"what tools does Claude Code have?"* or *"did any tools change in this release?"* and it will run this skill (see [SKILL.md](SKILL.md) for the exact triggers and every flag). You can always run the scripts directly instead.

> Pasting this repo's GitHub URL into an assistant does **not** install anything on its own. You (or an agent with shell access) must clone the repo and run the steps above.

## Privacy

Snapshots are generated locally and embed absolute paths from the machine that produced them, so `snapshots/*.json` is **gitignored** — captures never leave your machine. Each install builds its own history. To wipe it:

```bash
rm snapshots/*.json
```

## License

MIT — see [LICENSE](LICENSE).
