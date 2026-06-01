#!/usr/bin/env python3
"""Extract Claude Code tool registrations from on-disk binaries.

Three artifacts get scanned:

  1. System CLI binary at ~/.local/share/claude/versions/<ver>
  2. Desktop app's Electron bundle at /Applications/Claude.app/...
  3. Desktop app's embedded CLI at ~/Library/Application Support/
     Claude/claude-code/<ver>/ — the Electron shell ships its own
     separately-versioned claude-code binary internally and the Desktop
     surface = bundle ∪ embedded CLI.

Run with no arguments to scan your installed Claude Code and print, as
JSON, which Anthropic tools are bundled in each binary right now. That
works standalone — no API key, no companion, no network.

Two OPTIONAL inputs sharpen the "what changed" story over time:

  - Prior snapshots under snapshots/ — the history (catches removals).
  - Tap files from the optional monitor-claude-code companion skill —
    live discovery from the API registry (catches additions the moment
    a new tool ships, without brittle static heuristics).

When either is present, the scan is checked against it and the result is
labelled by provenance. When neither is present (a fresh clone), the
skill simply reports what it finds in the binaries. Nothing here requires
the companion to be installed.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# The set of "tools we expect to find" is not a hardcoded list — it's
# the union of:
#   1. Tools observed in every prior snapshot under snapshots/
#   2. Tools registered in any tap file produced by the (optional)
#      monitor-claude-code companion skill
#
# (1) gives us the history (catches removals). (2) gives us discovery
# (catches additions without needing brittle static-analysis heuristics
# on the bun-compiled CLI binary).
#
# BOTH inputs are optional. With neither, this skill still does its core
# job: scan the on-disk binaries and report which Anthropic tools are
# bundled right now. The taps and the design-reference archive are pure
# enrichment — when they're absent (e.g. a fresh clone with no companion
# installed), every code path below degrades gracefully instead of erroring.
#
# Heuristics like "find CamelCase strings near description markers"
# were tried and abandoned: the CLI binary is 200MB of bun-compiled
# JS with strings stored as UTF-16, and any proximity window collides
# with class names, library types, error messages, etc. False positive
# rate is ~95%. Better to lean on captured taps for ground truth.


def _resolve_companion_dir(env_var: str, candidates: list[Path]) -> Path:
    """Resolve an optional companion directory.

    Order: explicit env override → first existing candidate → first
    candidate as a nominal default. Callers must tolerate the returned
    path not existing, because everything read from these dirs is
    optional enrichment (see module docstring).
    """
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


# Optional discovery channel: tool names captured live from the API by the
# monitor-claude-code companion skill (https://github.com/liuaudi/monitor-claude-code).
# Entirely optional. Point it elsewhere with CLAUDE_CODE_TOOLS_TAPS, or
# ignore it — with no taps, the skill reports what it finds in the binaries.
MONITOR_TAPS = _resolve_companion_dir(
    "CLAUDE_CODE_TOOLS_TAPS",
    [
        Path.home() / ".claude" / "skills" / "monitor-claude-code" / "taps",
        Path.home() / "Code" / "skills" / "monitor-claude-code" / "taps",
    ],
)

# Optional design-reference archive (full system prompt + every tool's
# description + parameter schemas), produced by monitor-claude-code's
# exporter. Used only to report reference staleness and to power `--open`.
# Absent on a fresh clone — those features then report cleanly that there's
# nothing to show. Override the location with CLAUDE_CODE_TOOLS_REFERENCE.
REFERENCE_DIR = _resolve_companion_dir(
    "CLAUDE_CODE_TOOLS_REFERENCE",
    [
        Path.home() / ".claude" / "skills" / "_reference",
        Path.home() / "Code" / "skills" / "_reference",
    ],
)
# Common JS / runtime non-tools to filter from any name lists we read.
NOISE = frozenset({
    "Object", "Array", "String", "Number", "Boolean", "Promise", "Error",
    "Map", "Set", "Date", "RegExp", "Buffer", "Symbol", "Function",
    "Math", "JSON", "Reflect", "Proxy", "WeakMap", "WeakSet",
})

# Functional groupings used by --table mode. Order is preserved in the
# output. Each entry is (group label, [tool names in that group]).
GROUPS: list[tuple[str, list[str]]] = [
    ("File", ["Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"]),
    ("Shell", ["Bash", "PowerShell"]),
    ("Process", ["Monitor"]),
    ("Web", ["WebFetch", "WebSearch"]),
    ("Subagent", ["Agent", "Task"]),
    ("Background tasks", ["TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop"]),
    ("Cron", ["CronCreate", "CronDelete", "CronList"]),
    ("Plan mode", ["EnterPlanMode", "ExitPlanMode"]),
    ("Worktree", ["EnterWorktree", "ExitWorktree"]),
    ("Skill / ToolSearch", ["Skill", "ToolSearch"]),
    ("MCP", ["ListMcpResourcesTool", "ReadMcpResourceTool"]),
    ("Notifications/scheduling", ["AskUserQuestion", "TodoWrite", "PushNotification", "RemoteTrigger", "ScheduleWakeup"]),
    ("Onboarding", ["ShareOnboardingGuide"]),
]

# Baked-in baseline of known Anthropic tool names, derived from GROUPS.
# This is what lets the skill work on a FRESH CLONE with no taps and no
# snapshot history: `scan()` can only confirm names it already expects, so
# without a seed it would report nothing. The baseline gives every install
# a meaningful out-of-the-box scan ("which of these known tools are bundled
# in my binary, and which are missing?"). Taps and snapshot history extend
# it over time; tools beyond this set still surface via taps and the
# "observed but not grouped" footer. Keep it in sync with GROUPS.
BASELINE_TOOLS: frozenset[str] = frozenset(t for _, tools in GROUPS for t in tools)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, **kw)


CLI_FALLBACK_PATHS = [
    Path.home() / ".local" / "bin" / "claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
]

# monitor-claude-code's install-cli-shim.sh / install-desktop-shim.sh wrap the
# real Claude Code binary with a small shell script that conditionally
# routes through the local proxy. We must inspect the real binary, not
# the shim, or we'd report every tool as missing — the shim is ~10 lines
# of shell with zero tool names.
#
# The shim embeds its backup path as a `# Real binary: <abs path>` line
# in the first kilobyte. We read it out instead of inferring sibling
# location, because the CLI shim's backup lives OUTSIDE versions/
# (Claude's self-cleanup would otherwise delete it) while the Desktop
# shim keeps its backup next to the binary as `.real`. One discovery
# mechanism handles both, and future relocations of the backup don't
# require updating this code.
_SHIM_MARKER = b"monitor-claude-code shim"
_SHIM_REAL_RE = re.compile(rb"^# Real binary:\s*(.+)$", re.MULTILINE)


def resolve_shimmed_binary(path: str) -> str:
    """If `path` is a monitor-claude-code shim, return the path to the real
    binary it wraps. Otherwise return `path` unchanged.

    The real binary's path comes from the shim's own header comment.
    Falls back to `<path>.real` sibling for compatibility with the
    Desktop shim's convention. Returns `path` unchanged if no backup
    is found (e.g., shim install was destroyed by a cleanup pass) —
    callers will then scan the shim itself and report missing tools,
    which is the truthful state.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return path
    if _SHIM_MARKER not in head:
        return path
    m = _SHIM_REAL_RE.search(head)
    if m:
        candidate = m.group(1).strip().decode("utf-8", "replace")
        if Path(candidate).is_file():
            return candidate
    # Fallback: legacy sibling convention (Desktop shim still uses it).
    sibling = path + ".real"
    if Path(sibling).is_file():
        return sibling
    return path


def find_cli() -> tuple[str | None, str | None, str | None]:
    """Return (binary path, resolved real path, version string).

    Tries `which claude` first, then a handful of known install paths.
    The fallback matters when this runs under launchd, whose minimal
    default PATH (/usr/bin:/bin:/usr/sbin:/sbin) won't find ~/.local/bin
    — without the fallback, auto-snapshots would record a false "all
    tools removed" event on every system update.
    """
    path = shutil.which("claude")
    if not path:
        for candidate in CLI_FALLBACK_PATHS:
            if candidate.exists():
                path = str(candidate)
                break
    if not path:
        return None, None, None
    real = os.path.realpath(path)
    # If the on-disk binary has been wrapped by monitor-claude-code's shim,
    # scan its .real backup instead. The shim itself contains no tool
    # strings, so without this redirect we'd false-report all tools as
    # removed from the CLI surface.
    real = resolve_shimmed_binary(real)
    version = ""
    try:
        out = run([path, "--version"], timeout=10)
        version = out.stdout.decode("utf-8", "replace").strip().splitlines()[0]
    except Exception:
        pass
    return path, real, version


def find_desktop() -> tuple[str | None, str | None]:
    """Return (.app path, version string)."""
    candidates = [
        Path("/Applications/Claude.app"),
        Path.home() / "Applications" / "Claude.app",
    ]
    for p in candidates:
        if p.is_dir():
            info = p / "Contents" / "Info.plist"
            version = ""
            try:
                with open(info, "rb") as f:
                    plist = plistlib.load(f)
                version = plist.get("CFBundleShortVersionString", "")
            except Exception:
                pass
            return str(p), version
    return None, None


def find_desktop_embedded_cli() -> tuple[str | None, str | None]:
    """Locate the claude-code binary that the Desktop app ships internally.

    Discovered empirically: when the Desktop app launches a Claude Code
    session, its tool registry is the merger of the Electron bundle and
    a separately-versioned claude-code binary stored under
    ~/Library/Application Support/Claude/claude-code/<version>/.
    This binary is what contributes tools like ScheduleWakeup,
    PushNotification, EnterPlanMode etc. that don't appear in the main
    JS bundle. Without scanning it we'd report those as "missing" from
    the Desktop surface, which is wrong — they ship, just elsewhere.

    Returns (binary_path, version_string) or (None, None).
    """
    base = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
    if not base.is_dir():
        return None, None
    # Pick the highest-versioned dir (lexicographic is fine for semver-ish strings)
    versions = sorted([p for p in base.iterdir() if p.is_dir()], reverse=True)
    for vdir in versions:
        bin_path = vdir / "claude.app" / "Contents" / "MacOS" / "claude"
        if bin_path.is_file():
            # Same shim-aware redirect as the system CLI path. The Desktop
            # shim wraps this binary identically; without checking, the
            # embedded-CLI scan would come up empty and Desktop-only tools
            # like ScheduleWakeup/PushNotification/EnterPlanMode would
            # falsely report as missing.
            return resolve_shimmed_binary(str(bin_path)), vdir.name
    return None, None


def extract_strings(path: str) -> bytes:
    """Run `strings -n 4` on a binary, return raw bytes."""
    try:
        out = run(["strings", "-n", "4", path], timeout=120)
        return out.stdout
    except Exception:
        return b""


def read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return b""


def scan(blob: bytes, *, expected: set[str], line_exact: bool) -> dict:
    """Check which of `expected` tool names appear in the blob.

    `expected` comes from the union of snapshot history and monitor-claude-code
    tap files — i.e. tools we have *empirical* evidence are real, not a
    hardcoded list. For each name in `expected`, we check whether its
    bytes appear as a standalone string (line_exact) or bounded token
    (word-boundary) in the blob.

    Returns:
      - present: subset of expected that's found in this blob
      - missing_vs_expected: subset of expected NOT found (= removals
        for this surface, if we trust expected as the ground truth)
    """
    if line_exact:
        lines = set(blob.split(b"\n"))
        present = {t for t in expected if t.encode() in lines}
    else:
        present = {
            t for t in expected
            if re.search(rb'(?<![A-Za-z0-9_])' + re.escape(t.encode()) + rb'(?![A-Za-z0-9_])', blob)
        }
    return {
        "present": sorted(present),
        "missing_vs_expected": sorted(expected - present),
    }


def tools_from_taps(taps_dir: Path) -> dict[str, set[str]]:
    """Read every tap file and return tool names grouped by detected surface.

    Returns a dict like {"cli": {...}, "desktop": {...}} where each value
    is the union of tool names seen in all taps for that surface. Surface
    is detected from the cc_entrypoint header in the tap's system prompt.
    MCP tools (names starting with mcp__) are excluded — they're user-
    specific connector tools, not Anthropic-built tools we should track.
    """
    out: dict[str, set[str]] = {"cli": set(), "desktop": set()}
    if not taps_dir.is_dir():
        return out
    for tap in taps_dir.glob("*.json"):
        try:
            data = json.loads(tap.read_text())
        except Exception:
            continue
        req = data.get("request") or data
        sys_field = req.get("system") or []
        sys_text = ""
        if isinstance(sys_field, str):
            sys_text = sys_field
        else:
            for b in sys_field:
                if isinstance(b, dict):
                    sys_text += b.get("text") or ""
        # The cc_entrypoint value drifts across releases: older taps used
        # `sdk-cli` / `claude-code` / `claude-desktop`, current ones emit a
        # bare `cli` / `desktop`. Extract the value once and map robustly so
        # a naming change doesn't silently drop every tap (which would make
        # the tap discovery channel return nothing — see normalize_surface
        # in monitor-claude-code's export_reference.py for the canonical map).
        surface = "unknown"
        m = re.search(r"cc_entrypoint=([\w-]+)", sys_text)
        if m:
            ep = m.group(1)
            if "desktop" in ep:
                surface = "desktop"
            elif ep in {"cli", "sdk-cli", "claude-code"}:
                surface = "cli"
        if surface == "unknown":
            continue
        for tool in req.get("tools") or []:
            name = tool.get("name") if isinstance(tool, dict) else None
            if name and not name.startswith("mcp__") and name not in NOISE:
                out[surface].add(name)
    return out


def ensure_desktop_bundle(app_path: str, version: str) -> str | None:
    """Extract the desktop app's app.asar to a versioned cache dir.
    Returns the path to the main JS bundle, or None.
    """
    asar = Path(app_path) / "Contents" / "Resources" / "app.asar"
    if not asar.is_file():
        return None
    cache = Path(os.environ.get("TMPDIR", "/tmp")) / "claude-code-tools-cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"desktop-{version or 'unknown'}"
    if not out.is_dir():
        # Use npx to extract — most users will have node available; if not,
        # we just skip the desktop side gracefully.
        try:
            run(
                ["npx", "--yes", "@electron/asar", "extract", str(asar), str(out)],
                timeout=120,
            )
        except Exception:
            return None
    bundle = out / ".vite" / "build" / "index.js"
    return str(bundle) if bundle.is_file() else None


# Reference filenames look like cc-reference-<surface>-<semver>[.<suffix>]-<utc>.{md,html}
# e.g. cc-reference-cli-2.1.138.2e1-20260511T004533Z.md
#      cc-reference-desktop-2.1.128.138-20260511T054025Z.md
# We pull the leading semver for staleness comparison against the live binary.
_REF_VERSION_RE = re.compile(r"cc-reference-\w+-(\d+\.\d+\.\d+)")
# Reference filename timestamps come in two formats:
#   Older captures:  20260511T004533Z       (UTC suffix `Z`)
#   Newer captures:  20260514T19225607-0700 / 20260514T19225607+0000
# The exporter switched at some point to local-time + tz offset. Accept both.
_REF_TIMESTAMP_RE = re.compile(
    r"-(\d{8}T\d{6}(?:Z|\d{4}|[+-]\d{4}))\.(?:md|html)$"
)


def latest_reference(surface: str, current_version: str | None = None) -> dict:
    """Resolve cc-reference-<surface>.md to its current versioned target.

    Returns a dict describing the reference and how it compares to the
    live binary. Fields:
      - path: resolved real path of the .md (or None)
      - version: semver parsed from the filename (or None)
      - captured_at: UTC timestamp from filename (or None)
      - binary_version: the live binary's version we're comparing against
      - matches_binary: True/False/None when comparable
      - stale_note: human-readable warning when versions disagree
      - note: explanation when there's no reference at all

    The skill's whole reason to point at a reference is so an author can
    read what tool prose looked like when this surface was captured. If
    the binary has moved on since, the reference is stale and should be
    refreshed — claude-code-tools surfaces that loudly so you don't accidentally
    imitate prose from a release that no longer ships.
    """
    link = REFERENCE_DIR / f"cc-reference-{surface}.md"
    target: Path | None = None
    out: dict = {"path": None}
    if link.is_symlink():
        try:
            target = link.resolve(strict=True)
        except (OSError, RuntimeError):
            return {"path": None, "note": f"symlink {link} is broken"}
    elif link.is_file():
        target = link
    if target is None:
        # Distinguish "the optional reference archive isn't installed at all"
        # (calm, expected on a fresh clone) from "the archive exists but this
        # surface hasn't been captured yet" (actionable).
        if not REFERENCE_DIR.exists():
            return {
                "path": None,
                "optional_absent": True,
                "note": (
                    "full prompt/tool-schema reference not installed (optional). "
                    "Add the monitor-claude-code companion to enable --open and "
                    "staleness checks."
                ),
            }
        return {
            "path": None,
            "note": (
                f"no reference at {link}. Run a {surface} Claude Code "
                "session through monitor-claude-code's proxy and export to "
                "populate one."
            ),
        }
    out["path"] = str(target)
    name = target.name
    m = _REF_VERSION_RE.match(name)
    ref_ver = m.group(1) if m else None
    out["version"] = ref_ver
    m2 = _REF_TIMESTAMP_RE.search(name)
    out["captured_at"] = m2.group(1) if m2 else None

    if current_version:
        # CLI --version returns "2.1.140 (Claude Code)"; we just want the
        # leading semver. Desktop's embedded CLI version is already bare.
        cur = current_version.split()[0]
        out["binary_version"] = cur
        if ref_ver:
            out["matches_binary"] = (ref_ver == cur)
            if ref_ver != cur:
                out["stale_note"] = (
                    f"reference is for {ref_ver}; binary is {cur}. "
                    f"Capture a fresh {surface} session through monitor-claude-code "
                    "and re-export to refresh."
                )
        else:
            out["matches_binary"] = None
    return out


def historical_tools(snapshot_dir: Path, surface: str) -> set[str]:
    """Union of every tool ever observed for `surface` across all
    historical snapshots. Reads both old-schema (`known_present` +
    `discovered_extra`) and new-schema (`present`) fields so the function
    keeps working as the snapshot format evolves."""
    seen: set[str] = set()
    for snap in snapshot_dir.glob("*.json"):
        try:
            data = json.loads(snap.read_text())
        except Exception:
            continue
        section = data.get(surface) or {}
        seen.update(section.get("present") or [])
        seen.update(section.get("known_present") or [])
        seen.update(section.get("discovered_extra") or [])
    return seen - NOISE


def expected_for(surface: str, snapshot_dir: Path, taps: dict[str, set[str]]) -> tuple[set[str], dict]:
    """Build the set of expected tool names for a surface, plus a
    provenance dict explaining where each name came from.

    Provenance is useful so consumers (and the user) can see whether a
    given tool entry is backed by historical snapshots, by live tap
    files, or both. Tools backed only by taps are very fresh; tools
    backed only by snapshots are historical.
    """
    from_baseline = BASELINE_TOOLS - NOISE
    from_history = historical_tools(snapshot_dir, surface)
    from_taps = taps.get(surface, set()) - NOISE
    expected = from_baseline | from_history | from_taps
    provenance = {
        "from_baseline": sorted(from_baseline),
        "from_snapshots": sorted(from_history),
        "from_live_taps": sorted(from_taps),
        "tap_only": sorted(from_taps - from_history - from_baseline),
        "snapshot_only": sorted(from_history - from_taps - from_baseline),
    }
    return expected, provenance


def inspect(snapshot_dir: Path) -> dict:
    taps = tools_from_taps(MONITOR_TAPS)
    cli_expected, cli_prov = expected_for("cli", snapshot_dir, taps)
    desk_expected, desk_prov = expected_for("desktop", snapshot_dir, taps)

    # CLI and embedded-CLI scanning both shell out to `strings`. If it isn't
    # installed (a Mac without Xcode Command Line Tools), those scans return
    # nothing — which would wrongly read as "all tools removed". Detect it once
    # so we can render an honest "couldn't scan" state instead.
    strings_ok = shutil.which("strings") is not None

    cli_link, cli_real, cli_ver = find_cli()
    cli_installed = cli_link is not None
    cli_section: dict = {
        "path": cli_link,
        "resolved_path": cli_real,
        "version": cli_ver,
        "installed": cli_installed,
        "reference": latest_reference("cli", cli_ver),
        "expected_provenance": cli_prov,
    }
    if cli_real and Path(cli_real).is_file() and strings_ok:
        cli_section.update(scan(extract_strings(cli_real), expected=cli_expected, line_exact=True))
    elif cli_real and Path(cli_real).is_file():
        # Binary is right there but we can't read it — `strings` is missing.
        cli_section.update(present=[], missing_vs_expected=[], scan_unavailable=True)
    elif cli_installed:
        # We found a `claude` launcher on PATH but couldn't read the real
        # binary behind it (e.g. it's a wrapper script, not the bun binary).
        # Report the expected tools as unconfirmed — not as removals.
        cli_section.update(present=[], missing_vs_expected=sorted(cli_expected))
    else:
        # No Claude Code CLI on this machine at all. Don't pretend every
        # known tool was "removed" — there's simply nothing to scan.
        cli_section.update(present=[], missing_vs_expected=[])

    app_path, app_ver = find_desktop()
    embed_bin, embed_ver = find_desktop_embedded_cli()
    desktop_installed = bool(app_path or embed_bin)
    # The Desktop reference is keyed against the embedded CLI's version
    # (that's the binary monitor-claude-code actually captured), not the
    # Electron shell's Marketing version. So compare ref_ver to embed_ver.
    desktop_section: dict = {
        "path": app_path,
        "version": app_ver,
        "embedded_cli_path": embed_bin,
        "embedded_cli_version": embed_ver,
        "installed": desktop_installed,
        "reference": latest_reference("desktop", embed_ver),
        "expected_provenance": desk_prov,
    }
    bundle = ensure_desktop_bundle(app_path, app_ver) if app_path else None
    desktop_section["bundle"] = bundle

    # The Desktop surface = Electron bundle ∪ embedded CLI binary. Scan
    # both, union the results. Track which artifact contributed each
    # tool so the user can see the breakdown.
    bundle_present: set[str] = set()
    embed_present: set[str] = set()
    if bundle:
        bundle_scan = scan(read_bytes(bundle), expected=desk_expected, line_exact=False)
        bundle_present = set(bundle_scan["present"])
    if embed_bin and Path(embed_bin).is_file() and strings_ok:
        embed_scan = scan(extract_strings(embed_bin), expected=desk_expected, line_exact=True)
        embed_present = set(embed_scan["present"])
    elif embed_bin and Path(embed_bin).is_file():
        # Embedded CLI is present but `strings` is missing, so we could only
        # read the Electron bundle. Flag the partial scan for an honest note.
        desktop_section["scan_partial"] = True

    if bundle or embed_bin:
        all_present = bundle_present | embed_present
        desktop_section["present"] = sorted(all_present)
        desktop_section["missing_vs_expected"] = sorted(desk_expected - all_present)
        desktop_section["present_by_artifact"] = {
            "electron_bundle_only": sorted(bundle_present - embed_present),
            "embedded_cli_only": sorted(embed_present - bundle_present),
            "both": sorted(bundle_present & embed_present),
        }
    elif desktop_installed:
        # The .app exists but we couldn't read either artifact (e.g. no
        # Node/npx to unpack the Electron bundle, and no embedded CLI yet).
        # Report expected tools as unconfirmed rather than removed.
        desktop_section.update(present=[], missing_vs_expected=sorted(desk_expected))
    else:
        # No Claude Desktop app on this machine. Nothing to scan — don't
        # report the whole known tool set as "removed".
        desktop_section.update(present=[], missing_vs_expected=[])

    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cli": cli_section,
        "desktop": desktop_section,
    }


_TS_RE = re.compile(r"\d{8}T\d{6}Z")


def latest_snapshot(snapshot_dir: Path) -> Path | None:
    """Find the chronologically latest snapshot.

    Sorts by the UTC timestamp embedded in the filename rather than
    alphabetical filename order. Necessary because the version-first
    naming format puts the timestamp at the end and includes
    placeholders like `?` (for fields not captured yet) that ASCII-sort
    after digits — pure alphabetical sort would pick the wrong file as
    the diff baseline.
    """
    def ts_key(p: Path) -> str:
        m = _TS_RE.search(p.name)
        return m.group(0) if m else p.name
    snaps = list(snapshot_dir.glob("*.json"))
    return max(snaps, key=ts_key) if snaps else None


def _present_set(section: dict) -> set[str]:
    """Get the set of present tools, tolerant of old and new snapshot schemas."""
    return (
        set(section.get("present") or [])
        | set(section.get("known_present") or [])
        | set(section.get("discovered_extra") or [])
    )


def diff_tools(prev: dict, curr: dict) -> dict:
    """Diff two snapshot dicts; report adds/removes per surface."""
    out = {}
    for surface in ("cli", "desktop"):
        p_set = _present_set(prev.get(surface) or {})
        c_set = _present_set(curr.get(surface) or {})
        out[surface] = {
            "prev_version": (prev.get(surface) or {}).get("version"),
            "curr_version": (curr.get(surface) or {}).get("version"),
            "added": sorted(c_set - p_set),
            "removed": sorted(p_set - c_set),
        }
    return out


def _summarize_static(tools: list[str], present: set[str]) -> str:
    """Cell text for CLI/Desktop columns: '✅ all' / '✅ N (no X)' / '❌'."""
    have = [t for t in tools if t in present]
    miss = [t for t in tools if t not in present]
    if not have:
        return "❌"
    if not miss:
        return "✅ all" if len(tools) > 1 else "✅"
    return f"✅ {len(have)} (no {', '.join(miss)})"


def _summarize_live(tools: list[str], eager: set[str], deferred: set[str]) -> str:
    """Cell text for the live-session column.

    Classifies each tool as eager / deferred / absent and emits a compact
    summary like 'Read/Write eager; NotebookEdit deferred; Glob/Grep absent'.
    """
    if eager is None and deferred is None:
        return "(unknown)"
    by_class: dict[str, list[str]] = {"eager": [], "deferred": [], "absent": []}
    for t in tools:
        if t in eager:
            by_class["eager"].append(t)
        elif t in deferred:
            by_class["deferred"].append(t)
        else:
            by_class["absent"].append(t)
    classes = [c for c in ("eager", "deferred", "absent") if by_class[c]]
    if len(classes) == 1:
        only = classes[0]
        if only == "absent":
            return "❌"
        if len(tools) == 1:
            return only
        return f"all {only}"
    parts = []
    for cls in classes:
        names = "/".join(by_class[cls])
        parts.append(f"{names} {cls}")
    return "; ".join(parts)


def render_table(snapshot: dict, eager: set[str] | None, deferred: set[str] | None) -> str:
    cli_set = _present_set(snapshot.get("cli") or {})
    desk_set = _present_set(snapshot.get("desktop") or {})
    # Distinguish "surface not installed on this machine" from "installed
    # but a tool is absent". Default True keeps old snapshots (which predate
    # the `installed` field) rendering as before.
    cli_installed = (snapshot.get("cli") or {}).get("installed", True)
    desk_installed = (snapshot.get("desktop") or {}).get("installed", True)
    # The CLI was found but couldn't be read (e.g. `strings` is missing).
    # Render it as "?" with an explanatory note instead of an empty column
    # that would read as "all tools removed".
    cli_scannable = not (snapshot.get("cli") or {}).get("scan_unavailable")
    # The live-session column is only meaningful when the caller tells us
    # what the running session actually loaded (--eager / --deferred, usually
    # via the monitor-claude-code companion). In the common standalone case we
    # drop the column entirely rather than print one that's always "(unknown)".
    show_live = eager is not None or deferred is not None
    if show_live:
        rows = ["| Tool group | CLI binary | Desktop (bundle + embedded CLI) | Live session |",
                "|------|------------|----------------|--------------|"]
    else:
        rows = ["| Tool group | CLI binary | Desktop (bundle + embedded CLI) |",
                "|------|------------|----------------|"]
    for label, tools in GROUPS:
        first = f"{label}: {', '.join(tools)}"
        if not cli_installed:
            cli_cell = "—"
        elif not cli_scannable:
            cli_cell = "?"
        else:
            cli_cell = _summarize_static(tools, cli_set)
        desk_cell = _summarize_static(tools, desk_set) if desk_installed else "—"
        if show_live:
            live_cell = _summarize_live(tools, eager or set(), deferred or set())
            rows.append(f"| {first} | {cli_cell} | {desk_cell} | {live_cell} |")
        else:
            rows.append(f"| {first} | {cli_cell} | {desk_cell} |")
    # Trailing footnote: tools observed in this run but not represented in
    # any group above. These are the candidates for adding to GROUPS.
    grouped = {t for _, ts in GROUPS for t in ts}
    observed = cli_set | desk_set
    ungrouped = sorted(observed - grouped)
    footer = ""
    if ungrouped:
        footer = "\n\n_Observed but not grouped:_ " + ", ".join(f"`{t}`" for t in ungrouped)
    desktop_ver = snapshot["desktop"].get("version") or "?"
    embed_ver = snapshot["desktop"].get("embedded_cli_version")
    desktop_str = f"Desktop {desktop_ver}" + (f" (embedded CLI {embed_ver})" if embed_ver else "")
    versions = (
        f"\n\n_CLI {snapshot['cli'].get('version') or '?'}_ · "
        f"_{desktop_str}_ · "
        f"_captured {snapshot['captured_at']}_"
    )
    # Reference staleness — loud, because using a stale reference for
    # skill-writing means imitating prose from a version that no longer
    # ships. The shim+proxy combo (monitor-claude-code) is the fix; we tell
    # the user exactly what to run.
    ref_lines: list[str] = []
    # If the optional reference archive isn't installed at all, say so once,
    # calmly — it's expected on a standalone install and shouldn't read like
    # a per-surface failure.
    refs = [(snapshot.get(s) or {}).get("reference") or {} for s in ("cli", "desktop")]
    if refs and all(r.get("optional_absent") for r in refs):
        ref_lines.append(f"ℹ️  {refs[0]['note']}")
    else:
        for surface, label, current_field in (
            ("cli", "CLI", "version"),
            ("desktop", "Desktop embedded CLI", "embedded_cli_version"),
        ):
            ref = (snapshot.get(surface) or {}).get("reference") or {}
            if ref.get("matches_binary") is True:
                ref_lines.append(
                    f"✅ {label} reference matches binary "
                    f"({ref.get('version')}, captured {ref.get('captured_at') or '?'})"
                )
            elif ref.get("matches_binary") is False:
                cur = ref.get("binary_version") or "?"
                rver = ref.get("version") or "?"
                ref_lines.append(
                    f"⚠️  **{label} reference is STALE** — captured for `{rver}`, "
                    f"binary is `{cur}`. Run `bash ~/.claude/skills/monitor-claude-code/scripts/start.sh` "
                    f"and `monitor-claude` (or relaunch Desktop) to refresh."
                )
            elif ref.get("optional_absent"):
                ref_lines.append(f"ℹ️  {label}: {ref['note']}")
            elif ref.get("note"):
                ref_lines.append(f"⚠️  {label}: {ref['note']}")
    ref_block = ("\n\n" + "\n".join(ref_lines)) if ref_lines else ""
    # Absent-surface notes — the single most common cross-machine surprise.
    # A `—` column means "this surface isn't installed here", NOT "every tool
    # was removed". Spell that out so nobody reads it as a regression.
    absent_lines: list[str] = []
    if not cli_installed:
        absent_lines.append(
            "ℹ️  No Claude Code **CLI** found on this machine — the CLI column "
            "shows `—` (not installed, not removed tools). Put `claude` on your "
            "PATH to scan it."
        )
    elif not cli_scannable:
        absent_lines.append(
            "⚠️  Found the Claude Code **CLI** but couldn't read it — the `strings` "
            "tool is missing, so the CLI column shows `?`. Install the Xcode "
            "Command Line Tools to fix it: `xcode-select --install`."
        )
    if not desk_installed:
        absent_lines.append(
            "ℹ️  No Claude **Desktop** app found on this machine — the Desktop "
            "column shows `—` (not installed, not removed tools)."
        )
    elif (snapshot.get("desktop") or {}).get("scan_partial"):
        absent_lines.append(
            "⚠️  Scanned only the Desktop **Electron bundle** — the `strings` tool "
            "is missing, so the embedded CLI wasn't read and a few tools may show "
            "as absent. Install the Xcode Command Line Tools: `xcode-select --install`."
        )
    absent_block = ("\n\n" + "\n".join(absent_lines)) if absent_lines else ""
    return "\n".join(rows) + footer + versions + absent_block + ref_block


def _parse_csv_arg(argv: list[str], flag: str) -> set[str] | None:
    """Pull `--flag a,b,c` (or `--flag=a,b,c`) out of argv. Returns set or None."""
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return {s.strip() for s in argv[i + 1].split(",") if s.strip()}
        if arg.startswith(flag + "="):
            return {s.strip() for s in arg[len(flag) + 1:].split(",") if s.strip()}
    return None


def main(argv: list[str]) -> int:
    snapshot_dir = Path(__file__).resolve().parent.parent / "snapshots"
    snapshot_dir.mkdir(exist_ok=True)

    # Default behavior is save + diff — the whole point of this skill is
    # tracking changes over time, so it should be friction-free. Opt out
    # with --no-save / --no-diff when you want a one-shot peek.
    save = "--no-save" not in argv
    force_save = "--force-save" in argv
    show_diff = "--no-diff" not in argv
    table_mode = "--table" in argv or "-t" in argv
    eager = _parse_csv_arg(argv, "--eager")
    deferred = _parse_csv_arg(argv, "--deferred")

    # --open / --open=cli / --open=desktop: launch the corresponding
    # cc-reference-*.html in the user's default browser. HTML has
    # navigation, search, and copy-as-JSON buttons; the .md is for
    # programmatic / agent context. If you specifically want the .md
    # (e.g. to read it without a browser), use --open-md.
    #
    # The reference symlinks are maintained by monitor-claude-code's exporter;
    # if neither exists, the user hasn't run a capture yet — say so cleanly
    # rather than failing.
    open_target: str | None = None
    open_ext: str = "html"
    for arg in argv:
        if arg == "--open":
            open_target = "cli"
        elif arg.startswith("--open="):
            open_target = arg.split("=", 1)[1]
        elif arg == "--open-md":
            open_target = "cli"
            open_ext = "md"
        elif arg.startswith("--open-md="):
            open_target = arg.split("=", 1)[1]
            open_ext = "md"
    if open_target:
        ref_path = REFERENCE_DIR / f"cc-reference-{open_target}.{open_ext}"
        if not ref_path.exists():
            # Fall back to the other format if available
            alt_ext = "md" if open_ext == "html" else "html"
            alt_path = REFERENCE_DIR / f"cc-reference-{open_target}.{alt_ext}"
            if alt_path.exists():
                print(
                    f"note: no {open_ext} reference at {ref_path}, "
                    f"falling back to {alt_ext}",
                    file=sys.stderr,
                )
                ref_path = alt_path
            else:
                print(
                    f"error: no reference at {ref_path}.\n"
                    f"Run monitor-claude-code and export a {open_target} capture first.",
                    file=sys.stderr,
                )
                return 2
        subprocess.run(["open", str(ref_path)])
        try:
            resolved = ref_path.resolve(strict=True)
            print(f"opened {ref_path.name} → {resolved.name}", file=sys.stderr)
        except (OSError, RuntimeError):
            print(f"opened {ref_path}", file=sys.stderr)
        return 0

    snapshot = inspect(snapshot_dir)

    # Compute diff against the latest existing snapshot first — we use it
    # for two purposes: (1) the output `_diff` block, (2) deciding whether
    # this snapshot is worth saving.
    prior_path = latest_snapshot(snapshot_dir)
    diff = None
    if prior_path:
        prior_data = json.loads(prior_path.read_text())
        diff = diff_tools(prior_data, snapshot)

    def diff_is_empty(d):
        if not d:
            return False  # no prior → treat as "interesting" so first run saves
        return all(
            not v.get("added") and not v.get("removed") and v.get("prev_version") == v.get("curr_version")
            for v in d.values()
        )

    if save and (force_save or prior_path is None or not diff_is_empty(diff)):
        # Filename format: version-first, timestamp-last. This makes the
        # directory listing self-organizing — captures cluster by version
        # in alphabetical order, and within a cluster they sort by time.
        #   cli-<system CLI>__desktop-<Electron>__embed-<Desktop's
        #   internal CLI>__<UTC>.json
        # We take only the version *number* (e.g. "2.1.138"), not the
        # parenthetical product name, to keep the name tight.
        ts = snapshot["captured_at"].replace(":", "").replace("-", "")
        cli_v = (snapshot["cli"].get("version") or "?").split()[0] or "?"
        desk_v = snapshot["desktop"].get("version") or "?"
        embed_v = snapshot["desktop"].get("embedded_cli_version") or "?"
        path = snapshot_dir / f"cli-{cli_v}__desktop-{desk_v}__embed-{embed_v}__{ts}.json"
        path.write_text(json.dumps(snapshot, indent=2))
        snapshot["_saved_to"] = str(path)
    elif save:
        snapshot["_saved_to"] = None
        snapshot["_save_skipped"] = (
            f"no change vs {prior_path.name} — snapshot not persisted"
        )

    if show_diff:
        if prior_path is None:
            snapshot["_diff"] = {"note": "no prior snapshot to diff against"}
        else:
            snapshot["_diff"] = {"against": prior_path.name, **diff}

    if table_mode:
        print(render_table(snapshot, eager, deferred))
        return 0

    print(json.dumps(snapshot, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
