#!/usr/bin/env bash
# Uninstall the launchd LaunchAgent installed by install-watcher.sh.
set -euo pipefail

LABEL="local.claude-code-tools.auto-snapshot"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "${DOMAIN}" "${PLIST}" 2>/dev/null || true
  echo "unloaded ${LABEL}"
fi

if [[ -f "$PLIST" ]]; then
  rm "$PLIST"
  echo "removed ${PLIST}"
fi

echo "done."
