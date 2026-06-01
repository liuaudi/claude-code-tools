#!/usr/bin/env bash
# Run the inspector on a launchd WatchPaths event. Writes a snapshot iff
# something changed (smart-save handles dedup), logs to ~/Library/Logs/,
# and surfaces a macOS notification on real changes so the user knows.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSPECT="$SCRIPT_DIR/inspect.py"

# Optional: the monitor-claude-code companion's exporter refreshes the full
# design-reference archive from the latest captured tap. Entirely optional —
# if it isn't installed, the reference-refresh step is skipped cleanly.
# Override the location with CLAUDE_CODE_TOOLS_EXPORT_REF.
EXPORT_REF="${CLAUDE_CODE_TOOLS_EXPORT_REF:-}"
if [[ -z "$EXPORT_REF" ]]; then
  for _c in \
    "$HOME/.claude/skills/monitor-claude-code/scripts/export_reference.py" \
    "$HOME/Code/skills/monitor-claude-code/scripts/export_reference.py"; do
    if [[ -f "$_c" ]]; then EXPORT_REF="$_c"; break; fi
  done
fi

LOG="$HOME/Library/Logs/claude-code-tools-auto.log"
mkdir -p "$(dirname "$LOG")"

# launchd inherits a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that
# doesn't include ~/.local/bin or /opt/homebrew/bin, where `claude` and
# `npx` typically live. Augment PATH so the inspector can find them.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# A few seconds of slack: WatchPaths events fire mid-write during a bulk
# bundle replacement, when .app/Contents/Info.plist may not yet exist.
# A short delay lets the installer settle before we read the disk state.
sleep 5

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[$ts] WatchPaths event — running inspector" >> "$LOG"

python3 - "$INSPECT" "$EXPORT_REF" "$LOG" <<'PY'
import json
import os
import re
import subprocess
import sys

inspect_path, export_ref_path, log_path = sys.argv[1], sys.argv[2], sys.argv[3]

def log(msg: str) -> None:
    with open(log_path, "a") as f:
        f.write(msg)
        if not msg.endswith("\n"):
            f.write("\n")

# ── Step 1: snapshot the binaries (claude-code-tools inspector) ───────
inspect_result = subprocess.run(
    [sys.executable, inspect_path],
    capture_output=True,
    text=True,
)
log(inspect_result.stdout)
if inspect_result.stderr:
    log(inspect_result.stderr)

try:
    snapshot = json.loads(inspect_result.stdout)
except Exception:
    snapshot = {}

snapshot_saved = bool(snapshot.get("_saved_to"))
diff = snapshot.get("_diff") or {}
cli_v = (snapshot.get("cli") or {}).get("version") or "?"
desk_v = (snapshot.get("desktop") or {}).get("version") or "?"
embed_v = (snapshot.get("desktop") or {}).get("embedded_cli_version") or "?"

# Build the snapshot's change summary
changes = []
for surface in ("cli", "desktop"):
    s = diff.get(surface) or {}
    for added in s.get("added") or []:
        changes.append(f"{surface} +{added}")
    for removed in s.get("removed") or []:
        changes.append(f"{surface} -{removed}")

# ── Step 2: refresh the design reference if a fresh tap is available ──
# The reference exporter has its own smart-skip (no-op if the rendered
# .md would be byte-identical to what's already on disk), so we can call
# it unconditionally. It picks the latest tap. If no tap exists yet (e.g.
# the user hasn't started a Claude Code session since the new version
# installed), it exits cleanly without writing anything.
ref_changes = []
if os.path.isfile(export_ref_path):
    export_result = subprocess.run(
        [sys.executable, export_ref_path],
        capture_output=True,
        text=True,
    )
    log("--- export_reference.py ---\n" + export_result.stdout)
    if export_result.stderr:
        log(export_result.stderr)
    # The exporter writes "archived <file>" to stderr when it actually
    # wrote a new reference, vs "no change for surface=..." when it
    # skipped. Detect both.
    out = (export_result.stdout or "") + "\n" + (export_result.stderr or "")
    for m in re.finditer(r"archived (cc-reference-[\w.\-]+-[\w]+\.md)", out):
        ref_changes.append(m.group(1).replace(".md", ""))
else:
    log(f"(no exporter at {export_ref_path} — skipping reference refresh)\n")

# ── Step 3: decide whether to notify ──────────────────────────────────
if not snapshot_saved and not ref_changes:
    sys.exit(0)

# Build a notification body that surfaces tool changes AND any reference
# refresh together. Keep it compact — macOS notifications truncate.
parts = []
if changes:
    parts.append("; ".join(changes[:4]))  # cap at 4 for legibility
elif snapshot_saved:
    parts.append(f"CLI {cli_v} · Desktop {desk_v}/{embed_v}")
if ref_changes:
    parts.append(f"reference refreshed ({len(ref_changes)} file)")
summary = " · ".join(parts) if parts else "Claude Code state updated"
summary_escaped = summary.replace('"', '\\"')
subprocess.run([
    "osascript",
    "-e",
    f'display notification "{summary_escaped}" with title "Claude Code tools changed"',
])
PY

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done" >> "$LOG"
