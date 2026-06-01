---
name: claude-code-tools
description: Lists the built-in tools registered in the installed Claude Code CLI binary AND the Claude desktop app bundle, then diffs against the most recent saved snapshot to surface tools that have been added or removed across releases. Use this skill whenever the user asks about Claude Code's tool inventory, asks to compare CLI vs Desktop tools, asks "what tools does Claude Code have", wants to track tool changes over time, suspects a tool was added or removed in a release, or wants to investigate abstraction changes between Anthropic's two Claude Code surfaces. Also trigger this when the user mentions auditing, snapshotting, or tracking the action space of either binary.
---

# claude-code-tools

Static analysis of how Anthropic ships Claude Code on macOS. Three artifacts get scanned:

- **CLI binary** — `~/.local/share/claude/versions/<ver>` (bun-compiled Mach-O), located via `which claude`
- **Desktop app — Electron shell** — `/Applications/Claude.app`, main bundle at `Contents/Resources/app.asar` → `.vite/build/index.js`
- **Desktop app — embedded CLI** — `~/Library/Application Support/Claude/claude-code/<ver>/claude.app/Contents/MacOS/claude`. The Desktop app ships its own separately-versioned claude-code binary internally, which contributes most of the "system" tools (ScheduleWakeup, PushNotification, EnterPlanMode, etc.) that don't appear in the Electron bundle. Without scanning this, the Desktop tool count would look artificially small.

The "Desktop surface" report is the union of (Electron bundle) ∪ (embedded CLI). The skill tracks which artifact each tool came from so you can see how the Desktop is composed.

It does NOT make API calls or talk to a running process — it only inspects what's installed on disk.

### Source of truth for "expected tools"

The scan checks the binaries against a set of expected tool names. That set is the union of up to three sources — only the first is required:

1. **Baseline (always present)** — a baked-in list of known Anthropic tool names, derived from the `GROUPS` table at the top of `scripts/inspect.py`. This is what makes the skill work on a fresh clone with no history: `scan()` can only *confirm* names it already expects, so without a seed it would report nothing. The baseline gives every install a meaningful out-of-the-box result.
2. **Snapshot history (optional)** — every tool ever observed in any prior snapshot under `snapshots/`. Grows automatically as you save snapshots; catches removals.
3. **Live taps (optional)** — every tool name seen in `request.tools[]` of any captured API request, supplied by the [monitor-claude-code](https://github.com/liuaudi/monitor-claude-code) companion skill, grouped by surface via `cc_entrypoint`. This is the discovery channel: it learns a brand-new tool the moment it appears in an API request, before it's in any snapshot. Looked up at `~/.claude/skills/monitor-claude-code/taps/` or `~/Code/skills/monitor-claude-code/taps/` by default; override with `CLAUDE_CODE_TOOLS_TAPS`.

The companion is complementary, not required: it taps the live API and learns what tools exist (ground truth from the registry); this skill confirms which of those are still bundled in the on-disk binaries. With the companion, additions surface the moment they're captured. Without it, the baseline plus your own accumulating snapshots still track presence and removals.

## When to run

Run on demand. The user typically wants this when:
- Comparing CLI vs Desktop tool sets
- Verifying whether a tool they remember is still shipped
- After upgrading either surface, to record the new tool inventory
- Investigating why a tool exists in one surface but not the other

## How to use

The work happens in `scripts/inspect.py`. There's no need to re-implement the logic — call the script and present the output.

### Default invocation — just run it

```bash
python3 "$SKILL_DIR/scripts/inspect.py"
```

By default the script does **save + diff** on every invocation, because tracking change over time is the whole point of this skill. Specifically:

- Inspects CLI binary and Desktop bundle, builds a fresh snapshot
- Computes a `_diff` block vs the most recent saved snapshot (added/removed tools per surface, version delta)
- **Smart-saves**: persists a new file under `snapshots/` *only if something actually changed* (or this is the first run). Idle invocations don't pile up identical files.

The JSON output includes `_saved_to` (path written) or `_save_skipped` (reason it wasn't), plus the `_diff` block. Read both to know what changed and whether you have a fresh history entry.

### Snapshot file naming

Snapshots use a format that encodes all three versions first, timestamp last, so the directory listing self-clusters by version era:

```
cli-2.1.138__desktop-1.6608.2__embed-2.1.128__20260510T235633Z.json
└─ system CLI ─┘└── Electron ──┘└embedded CLI┘└── UTC capture ───┘
```

- `cli-` → the system-installed CLI binary's version (`~/.local/bin/claude --version`)
- `desktop-` → the Claude.app Electron app version (CFBundleShortVersionString)
- `embed-` → the embedded claude-code binary the Desktop ships inside itself
- Trailing UTC timestamp keeps within-version captures sortable chronologically

A directory of snapshots looks like:

```
cli-2.1.137__desktop-1.5354.0__embed-?__20260509T102449Z.json
cli-2.1.137__desktop-1.5354.0__embed-?__20260509T102951Z.json     ← 2.1.137 era
cli-2.1.138__desktop-1.6608.2__embed-2.1.128__20260510T235144Z.json
cli-2.1.138__desktop-1.6608.2__embed-2.1.128__20260510T235633Z.json ← 2.1.138 era
cli-2.1.138__desktop-1.6608.2__embed-?__20260510T231207Z.json     ← first 2.1.138 capture, before embedded-CLI scanning was added
```

`?` means that field wasn't captured at the time — typically because a scan path was added in a later release of this skill. The directory is grandfathered: old format snapshots (date-first, CLI-only versioning) are still parsed correctly by the schema-tolerant loader, but every new save uses the version-first format. The `latest_snapshot()` helper sorts by the embedded UTC timestamp regardless of filename format, so diffs always go against the chronologically most recent capture, not the alphabetically last one.

### Snapshots link to the full reference corpus (optional)

A snapshot file itself is intentionally lightweight (~few KB) — it records *what tools exist* and *what versions*, not their descriptions or schemas. The full corpus (system prompt + every tool's description + parameter schemas) lives in an optional reference archive produced by the [monitor-claude-code](https://github.com/liuaudi/monitor-claude-code) companion. It's looked up at `~/.claude/skills/_reference/` or `~/Code/skills/_reference/` by default; override with `CLAUDE_CODE_TOOLS_REFERENCE`. Without the companion this archive simply doesn't exist, and the `reference` fields below report that cleanly.

Each snapshot includes a `reference.path` per surface that resolves to the cc-reference-*.md file that was current when the snapshot was captured. Use this to navigate from a snapshot to the full prose:

```json
{
  "cli": {
    "version": "2.1.138 (Claude Code)",
    "reference": {
      "path": ".../cc-reference-cli-2.1.138.de9-20260510T231310Z.md"
    },
    ...
  }
}
```

If `reference.path` is null, no monitor-claude-code capture has been done for that surface — the snapshot will have a `reference.note` explaining how to populate one. The two skills are deliberately split: this one tracks *which* tools exist and *when* they appear/disappear; the reference files preserve *what they looked like*. Together they give you both the index and the corpus.

### Opt-outs and overrides

```bash
python3 "$SKILL_DIR/scripts/inspect.py" --no-save     # diff only, don't persist
python3 "$SKILL_DIR/scripts/inspect.py" --no-diff     # save only, skip the diff block
python3 "$SKILL_DIR/scripts/inspect.py" --no-save --no-diff   # raw snapshot, no history interaction
python3 "$SKILL_DIR/scripts/inspect.py" --force-save  # save even if no change vs last snapshot
```

### Open the live prompt + tools

The full system prompt + tool descriptions + schemas live on disk in two formats — there's no need to re-run monitor-claude-code every time you want to read them:

- **`cc-reference.html`** — interactive view: left-nav grouped by category, live filter, per-tool "Copy JSON" button. Use this when *reading* or *grabbing schemas*.
- **`cc-reference.md`** — plain markdown: agent-friendly text. Use this when *@-referencing into /skill-creator* or feeding the corpus to another tool.

```bash
# Skill flags
python3 "$SKILL_DIR/scripts/inspect.py" --open            # opens cc-reference.html (default)
python3 "$SKILL_DIR/scripts/inspect.py" --open=desktop    # desktop HTML
python3 "$SKILL_DIR/scripts/inspect.py" --open-md         # opens cc-reference.md
python3 "$SKILL_DIR/scripts/inspect.py" --open-md=desktop

# Or just the raw file paths
open ~/Code/skills/_reference/cc-reference.html
open ~/Code/skills/_reference/cc-reference.md
```

If one format is missing for a surface (e.g. an old capture that only produced .md), `--open` falls back to the other format with a note. If neither exists, the skill prints an actionable error pointing you at monitor-claude-code to seed one.

The `cc-reference.html` and `cc-reference.md` symlinks always track the latest CLI capture; both are refreshed by monitor-claude-code's exporter on every new tap. Re-export only when Claude Code itself updates — claude-code-tools' `_diff` output will tell you when.

### Grouped markdown table (CLI vs Desktop vs live session)

```bash
python3 "$SKILL_DIR/scripts/inspect.py" --table \
  --eager "Read,Write,Edit,Bash,Agent,Skill,ToolSearch,ScheduleWakeup,..." \
  --deferred "AskUserQuestion,CronCreate,CronDelete,CronList,Monitor,NotebookEdit,..."
```

Emits a markdown table with columns **Tool · CLI binary · Desktop bundle · Live session**, grouped by function (file ops, shell, web, subagent, background tasks, cron, plan mode, worktree, MCP, notifications/scheduling, etc.). The grouping lives in `GROUPS` at the top of `scripts/inspect.py`.

CLI and Desktop cells are filled in automatically from the static analysis. The **Live session** cell is populated from `--eager` and `--deferred` — comma-separated lists of tool names. **Claude must read these from its own context before invoking the script:**

- **Eager tools**: the names that appear inside the `<functions>...</functions>` block at the top of the system prompt. Pass only the bare tool names (e.g. `Read`, not full schemas), and skip `mcp__*` entries — those aren't on the tracked list.
- **Deferred tools**: the names listed under "The following deferred tools are now available via ToolSearch" in the session-startup `<system-reminder>`. Same `mcp__*` filtering.

Anything in a tracked group that's in neither list is reported as `absent`. The live cell aggregates per group, e.g. `Read/Write/Edit eager; NotebookEdit deferred; Glob/Grep absent`. `--save`/`--diff` work alongside `--table` to persist the underlying snapshot at the same time.

`$SKILL_DIR` is wherever the skill lives — `~/Code/skills/claude-code-tools` if you authored it there, or `~/.claude/skills/claude-code-tools` if running from the installed location. Resolve it with `dirname` on the SKILL.md if needed.

## How to present the result

The raw JSON is fine for most users, but pull out the headline numbers in prose:

- **CLI version + present-tool count**
- **Desktop**: Electron version + embedded-CLI version + total present-tool count (union of both artifacts)
- **`present_by_artifact`** for Desktop — interesting because it shows which tools live in the bundle vs the embedded CLI. The "embedded_cli_only" set tells you which "system tools" the Electron shell delegates to the bundled CLI rather than implementing in JS.
- **`missing_vs_expected`** — tools that some empirical source (snapshot or monitor-claude-code tap) led us to expect, but that this scan didn't find. The most common cause is a tool being removed in a newer release (e.g., `RemoteTrigger` in CLI 2.1.138). Confirm by checking the `_diff` block.
- **`expected_provenance`** — splits expected names by source: `from_baseline` (the baked-in known set), `from_snapshots`, `from_live_taps`, `tap_only` (just learned from taps), `snapshot_only` (we've seen it historically but no recent tap evidence).
- **Diff block** — added/removed since last snapshot.

If the user asks why a tool is in one surface and not the other, lead with the data. Common asymmetries:

- A tool present in the system CLI binary but missing from the Desktop bundle is probably still in the Desktop because the embedded CLI ships it (check `present_by_artifact.embedded_cli_only`).
- The embedded CLI inside Desktop is often an *older version* than the user's system CLI (e.g., 2.1.128 vs 2.1.138), so tools removed in newer releases may still be present in Desktop.

## Detection method

`scripts/inspect.py` does one check per artifact: for each name in the expected set (history ∪ taps), does it appear as a bare token in this binary/bundle?

- For the CLI binary and the embedded Desktop CLI binary (both bun-compiled Mach-O), we shell out to `strings -n 4` and look for the name on its own line.
- For the Electron bundle (raw JS text), we use word-boundary regex on the raw bytes.

The old "discovery via JSON-name pattern + CamelCase proximity" heuristic was removed: it produced ~95% false positives on the bun-compiled CLI binary because strings are stored as UTF-16 and "description" appears in thousands of non-tool contexts. Discovery happens via monitor-claude-code taps instead — that's ground truth from the API registry.

If `missing_vs_expected` is suspiciously large (e.g., dozens of tools), one of the artifact paths probably failed to load — check the `bundle` and `embedded_cli_path` fields. The Desktop bundle requires `npx @electron/asar extract` on first run; subsequent runs use a cached extraction at `$TMPDIR/claude-code-tools-cache/desktop-<version>/`.

## Growing the corpus

There's nothing to maintain manually. Two ways the expected set grows:

1. **Run monitor-claude-code** through any Claude Code session. Its tap files automatically feed new tool names into claude-code-tools' expected set on the next invocation.
2. **Save a snapshot** (the default behavior of every invocation). The snapshot records every tool currently present, which next time becomes part of the historical expectations.

That's it. No hardcoded list, no curation, no drift.

## Automatic snapshots on install/update

A launchd watcher can run the inspector automatically whenever the CLI or Desktop binaries change on disk, so you never have to remember to capture a release. Install once:

```bash
bash "$SKILL_DIR/scripts/install-watcher.sh"
```

This writes `~/Library/LaunchAgents/local.claude-code-tools.auto-snapshot.plist` with `WatchPaths` set to:

- `/Applications/Claude.app` — fires when Desktop is updated
- `~/.local/share/claude/versions` — fires when system CLI is updated
- `~/Library/Application Support/Claude/claude-code` — fires when Desktop's embedded CLI is updated

When any path changes, launchd waits 60s (`ThrottleInterval`, to debounce noisy mid-install events) and runs `scripts/auto-snapshot.sh`. That wrapper:

1. Sets a sane PATH (launchd's default is too minimal to find `claude` and `npx`)
2. Sleeps 5s to let an in-progress install settle
3. Runs `inspect.py` (which save+diffs by default, smart-skipping if nothing changed)
4. Runs `~/Code/skills/monitor-claude-code/scripts/export_reference.py` to refresh the design reference (`.md` + `.html`) from the latest captured tap. Has its own smart-skip: if the rendered content would match the current reference (modulo the capture timestamp), nothing is written.
5. Surfaces a single combined macOS notification iff something actually changed — version delta, added/removed tools, and/or "reference refreshed"

So on every Claude Code install event you get up to two artifacts updated atomically: a new snapshot under `snapshots/` if tool inventory changed, and a new reference under `~/Code/skills/_reference/` if the eager/deferred breakdown changed. Both are idempotent — re-running on the same state produces no new files.

This is the version-bump-driven auto-refresh, not a per-tap one. Per-tap export would write hundreds of duplicate files per day; this writes one when something meaningful actually shifts.

Logs land at `~/Library/Logs/claude-code-tools-auto.log` (inspector output) and `~/Library/Logs/claude-code-tools-launchd.{out,err}` (launchd-level).

Verify or manually fire:

```bash
launchctl print "gui/$(id -u)/local.claude-code-tools.auto-snapshot"    # status
launchctl kickstart -k "gui/$(id -u)/local.claude-code-tools.auto-snapshot"   # test-fire
```

Uninstall:

```bash
bash "$SKILL_DIR/scripts/uninstall-watcher.sh"
```

### Why both inspector fallback AND wrapper PATH

This was a real bug caught by the install test: launchd's default `PATH=/usr/bin:/bin:/usr/sbin:/sbin` doesn't include `~/.local/bin`, so `shutil.which("claude")` returns nothing under launchd and the auto-snapshot would record all CLI tools as "removed". Defense in depth fixed it: `find_cli()` falls back to checking known install paths even with no `PATH`, and `auto-snapshot.sh` exports a richer `PATH` before invoking Python. Either alone would close the gap; both makes the failure mode hard to reach again.

## Limitations

- macOS only (paths are mac-specific). Trivial to extend to Linux/Windows.
- Static analysis catches what's *bundled* in the on-disk binaries. MCP server tools registered at runtime by claude.ai connectors aren't tracked here — they're surface-level user customization, not part of the canonical Anthropic tool set. monitor-claude-code taps WILL see them in API requests; this skill explicitly filters them (`mcp__*` prefix) when ingesting tap data.
- Bun-compiled CLI binaries are ~200MB each; `strings` takes a few seconds per binary. The Electron asar extraction is ~10s on first run per version, then cached.
- The embedded Desktop CLI version lags the user's system CLI (e.g., system 2.1.138 vs embedded 2.1.128). The Desktop app upgrades its embedded CLI on its own cadence. This is itself useful data — it explains why tools removed in newer CLI releases still surface in Desktop sessions for a while.
