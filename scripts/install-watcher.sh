#!/usr/bin/env bash
# Install (or reinstall) the launchd LaunchAgent that auto-snapshots
# whenever Claude Code's CLI or Desktop install paths change.
#
# Idempotent: safely reruns to update the plist after a script edit.
set -euo pipefail

LABEL="local.claude-code-tools.auto-snapshot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT="$SCRIPT_DIR/auto-snapshot.sh"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_OUT="$HOME/Library/Logs/claude-code-tools-launchd.out"
LOG_ERR="$HOME/Library/Logs/claude-code-tools-launchd.err"

# Sanity: the watch script must exist and be executable
chmod +x "$AGENT"

# Watch paths — directories whose contents change when an update lands.
# WatchPaths on a directory fires on any add/remove/rename of its
# immediate children, which is what we want for "new version installed".
WATCH=(
  "/Applications/Claude.app"
  "$HOME/.local/share/claude/versions"
  "$HOME/Library/Application Support/Claude/claude-code"
)

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG_OUT")"

# Build the WatchPaths XML fragment
watch_xml=""
for p in "${WATCH[@]}"; do
  watch_xml+="    <string>${p}</string>
"
done

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${AGENT}</string>
  </array>
  <key>WatchPaths</key>
  <array>
${watch_xml}  </array>
  <key>ThrottleInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>${LOG_OUT}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_ERR}</string>
</dict>
</plist>
EOF

echo "wrote ${PLIST}"

# Reload: bootout if already loaded, then bootstrap fresh
DOMAIN="gui/$(id -u)"
if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  echo "unloading existing job..."
  launchctl bootout "${DOMAIN}" "${PLIST}" 2>/dev/null || true
fi
echo "loading job..."
launchctl bootstrap "${DOMAIN}" "${PLIST}"

# Print verification
echo
echo "status:"
launchctl print "${DOMAIN}/${LABEL}" | grep -E '^\s*(state|program|watch paths)' || true

cat <<EOF

installed. The watcher will fire when any of these paths change:
  - /Applications/Claude.app                                   (Desktop update)
  - ~/.local/share/claude/versions                             (system CLI update)
  - ~/Library/Application Support/Claude/claude-code           (Desktop embedded CLI update)

Each fire runs the inspector and (only if something changed) saves a snapshot
and shows a notification. Logs:
  - inspector output:    ~/Library/Logs/claude-code-tools-auto.log
  - launchd stdout/err:  ${LOG_OUT}
                         ${LOG_ERR}
  - snapshots:           ${SCRIPT_DIR}/../snapshots/

To uninstall:
  bash "${SCRIPT_DIR}/uninstall-watcher.sh"

To trigger manually for testing:
  launchctl kickstart -k "${DOMAIN}/${LABEL}"
EOF
