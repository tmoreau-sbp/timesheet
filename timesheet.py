#!/usr/bin/python3
"""Work time per day from laptop activity, as a calendar or a list.

Activity comes from the macOS power log (`pmset -g log`):
  - WindowServer logs a UserIsActive line every 15 min with the time since the
    last keyboard/mouse input, and a line when input resumes after a timeout.
  - loginwindow logs unlocks; wakes "due to UserActivity" are user wakes.
  - Teams calls count as active even without input.
caffeinate, audio and video keep the display awake but are not input, so they
are ignored, and idle time before display sleep / system sleep is not counted.

Work is split into projects, set on the first run and changed with
--configure: folders and browser profiles each belong to a project or to
nothing ("other"); a path belongs to its most specific configured folder.
Signals: Claude Code prompts and sessions, Codex and Gemini CLI sessions,
VS Code (and Cursor, VSCodium, Windsurf) file saves, file changes and git
reflog in configured folders, browser visits (Chromium browsers, Firefox
family, Safari), Teams calls.
Each 5-min slot takes the project of the nearest signal within 15 minutes,
else of the browser page in front; Teams calls always count for the
meetings project.

A day runs from 03:00 to 03:00 the next day, so late-night work counts for the
evening it belongs to. These numbers can be changed with --settings.

macOS keeps only ~7 days of power log. The first run only sets up projects;
from the second run the tool asks once whether to keep a history (an hourly
cron job saving the power log, 1 year, at most ~12 MB), so longer calendars
stay measured.
Before the archive starts there is no keyboard/mouse data: blocks are then
estimated from the signals alone and marked with * (yellow on a terminal).

Usage:
  timesheet.py                 work, last 6 weeks as Mon-Sun calendar
  timesheet.py -w 2            last 2 weeks
  timesheet.py -n 7 --list     last 7 days, one row per active period
  timesheet.py --other         also show time that is not work
  timesheet.py --configure     projects, what belongs to them, history
  timesheet.py --settings      day start, rounding and other numbers
  timesheet.py --uninstall     remove the cron job and all saved data

Safari history needs Full Disk Access for the terminal app.
"""

import argparse
import bisect
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

VERSION = "dev"  # set to the tag by the release workflow
HOME = Path.home()
APP_SUPPORT = HOME / "Library/Application Support"
# Browser name -> user data folder, per history format.
CHROMIUM = {
    "Chrome": "Google/Chrome",
    "Chrome Beta": "Google/Chrome Beta",
    "Chrome Canary": "Google/Chrome Canary",
    "Chromium": "Chromium",
    "Edge": "Microsoft Edge",
    "Brave": "BraveSoftware/Brave-Browser",
    "Vivaldi": "Vivaldi",
    "Arc": "Arc/User Data",
    "Opera": "com.operasoftware.Opera",
    "Opera GX": "com.operasoftware.OperaGX",
}
FIREFOX = {
    "Firefox": "Firefox",
    "Zen": "zen",
    "LibreWolf": "librewolf",
    "Floorp": "Floorp",
    "Waterfox": "Waterfox",
}
# kind -> (table, time column, epoch, unit, column marking synced visits, local-only filter)
HISTORY = {
    "chromium": (
        "visits",
        "visit_time",
        datetime(1601, 1, 1, tzinfo=timezone.utc),
        timedelta(microseconds=1),
        "originator_cache_guid",
        "COALESCE(originator_cache_guid, '') = ''",
    ),
    "firefox": (
        "moz_historyvisits",
        "visit_date",
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        timedelta(microseconds=1),
        "source",
        "source <> 1",
    ),
    "safari": (
        "history_visits",
        "visit_time",
        datetime(2001, 1, 1, tzinfo=timezone.utc),
        timedelta(seconds=1),
        "origin",
        "origin = 0",
    ),
}
# kind -> query of (start, time in front, visit length) per local page visit, in
# HISTORY units, from `since`. Chromium counts a visible tab, Firefox active viewing.
IN_FRONT = {
    "chromium": "SELECT v.visit_time, c.total_foreground_duration, v.visit_duration"
    " FROM visits v JOIN context_annotations c ON c.visit_id = v.id"
    " WHERE v.visit_time >= ? AND c.total_foreground_duration > 0"
    " AND COALESCE(v.originator_cache_guid, '') = ''",
    "firefox": "SELECT created_at * 1000, total_view_time * 1000, (updated_at - created_at) * 1000"
    " FROM moz_places_metadata WHERE created_at * 1000 >= ? AND total_view_time > 0",
}
# Editors with VS Code's local history (one entries.json per saved file).
EDITORS = ("Code", "Code - Insiders", "Cursor", "VSCodium", "Windsurf")
OTHER = "other"
RESERVED = {"start", "other", "total", "meetings", "all work"}
CLAUDE_PROJECTS = HOME / ".claude/projects"
# Every prompt typed in Claude Code, with folder; kept beyond cleanupPeriodDays.
CLAUDE_HISTORY = HOME / ".claude/history.jsonl"
# Other AI agents: Codex keeps one JSONL per session, Gemini CLI one JSON per chat.
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
GEMINI_HOME = HOME / ".gemini"
DATA_DIR = HOME / "Library/Application Support/timesheet"
DEFAULT_ARCHIVE = DATA_DIR / "pmset-archive.log"
DEFAULT_CONFIG = DATA_DIR / "config.json"
# --settings: name -> (default, lowest, highest, question)
SETTINGS = {
    "weeks": (6, 1, 104, "weeks shown"),
    "day_start": (3, 0, 23, "hour a day starts (earlier work counts for the day before)"),
    "break": (10, 1, 120, "minutes without input that end a work block"),
    "min_block": (5, 0, 120, "shortest block kept, in minutes"),
    "step": (5, 1, 60, "rounding step, in minutes"),
    "window": (15, 1, 120, "minutes an activity signal covers around it"),
}
# Marks our line in the user's crontab.
CRON_TAG = "# timesheet: save power log"
# The archive keeps one year: about 12 MB at most.
ARCHIVE_DAYS = 366
# Not user input: the USB sound card fires volume-key events on plug/wake.
IGNORE_DEVICES = ("Sound Blaster",)
# History older than this is never needed.
EARLIEST = datetime(2000, 1, 1)
MEETING_RE = re.compile(r"Teams Call in progress")
SKIP_DIRS = {
    ".git",
    ".terraform",
    ".terragrunt-cache",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
}

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}) (.+?)(?:\t|\s{2,})(.*)$")
# PID 599(WindowServer) Summary UserIsActive "com.apple...tickle ..." 00:08:52
ASSERT_RE = re.compile(r'PID \d+\(([^)]*)\) (\w+) (\w+) "([^"]*)" (\d+):(\d\d):(\d\d)')
REFLOG_RE = re.compile(r"> (\d+) [+-]\d{4}\t")
STAMP_FMT = "%Y-%m-%d %H:%M:%S %z"
# Lines parse_pmset uses; everything else stays out of the archive.
KEEP_RE = re.compile(
    r"WindowServer\) \w+ UserIsActive|loginwindow\) Created UserIsActive"
    r"|Display is turned|^\S+ \S+ \S+ (?:Sleep|Wake)(?:\t| {2})|" + MEETING_RE.pattern
)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def local(dt):
    """Aware datetime -> naive local time."""
    return dt.astimezone().replace(tzinfo=None)


# --- activity: pmset log -----------------------------------------------------


def read_pmset(path):
    if path:
        return Path(path).read_text(errors="replace")
    try:
        out = subprocess.run(
            ["pmset", "-g", "log"], capture_output=True, text=True, errors="replace", check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"cannot run 'pmset -g log': {e}")
    if out.startswith("Error"):
        sys.exit(f"pmset: {out.strip()}")
    return out


def update_archive(path, text, save=True):
    """Merge the lines of text that parse_pmset uses into the archive.

    Returns all archived lines, oldest first; writes them only when save is set.
    """
    new = [ln.rstrip() for ln in text.splitlines() if LINE_RE.match(ln) and KEEP_RE.search(ln)]
    try:
        old = [ln for ln in path.read_text().splitlines() if ln]
    except FileNotFoundError:
        old = []
    except OSError as e:
        warn(f"archive unreadable, using live log only: {e}")
        return "\n".join(new)
    # One entry per event: the same event printed in another time zone (travel)
    # or with other column padding (macOS update) is not stored twice.
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_DAYS)
    events = {}
    for ln in old + new:
        when = datetime.strptime(ln[:25], STAMP_FMT).astimezone(timezone.utc)
        if when >= cutoff:
            events.setdefault((when, " ".join(ln[25:].split())), ln)
    # Stable sort on time keeps the log's own order within a second.
    lines = [ln for _, ln in sorted(events.items(), key=lambda kv: kv[0][0])]
    if lines == old or not save:
        return "\n".join(lines)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(path)
    except OSError as e:
        warn(f"archive not saved: {e}")
    return "\n".join(lines)


def cron_lines():
    """The user's crontab, [] if there is none."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return result.stdout.splitlines() if result.returncode == 0 else []


def is_ours(line):
    """Our cron line, also a hand-made one without the tag."""
    return CRON_TAG in line or ("timesheet.py" in line and "--collect" in line)


def cron_installed():
    return any(is_ours(line) for line in cron_lines())


def write_crontab(lines):
    result = subprocess.run(
        ["crontab", "-"], input="".join(f"{ln}\n" for ln in lines), capture_output=True, text=True
    )
    if result.returncode:
        sys.exit(
            f"crontab failed: {result.stderr.strip()}\n"
            "If macOS blocked it, give your terminal app Full Disk Access, "
            "or run this tool at least every 6 days instead."
        )


def install_cron(archive):
    """Hourly cron job running --collect (cron skips hours the Mac sleeps through)."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"0 * * * * /usr/bin/python3 {shlex.quote(str(Path(__file__).resolve()))} "
        f"--collect 2>{shlex.quote(str(archive.with_name('collect.log')))} {CRON_TAG}"
    )
    write_crontab([ln for ln in cron_lines() if not is_ours(ln)] + [line])
    print(
        f"Hourly cron job installed: saves the power log to {tilde(archive)} "
        "(1 year kept, at most ~12 MB)."
    )


def uninstall_cron():
    lines = cron_lines()
    kept = [ln for ln in lines if not is_ours(ln)]
    if len(kept) < len(lines):
        write_crontab(kept)
        print("Cron job removed.")


def uninstall(config_path, archive):
    """Remove the cron job and every file the tool saved, after confirmation."""
    files = [config_path, archive, archive.with_suffix(".tmp"), archive.with_name("collect.log")]
    files = [f for f in dict.fromkeys(files) if f.exists()]
    print("This removes:")
    if cron_installed():
        print("  the hourly cron job")
    for f in files:
        print(f"  {tilde(f)} ({f.stat().st_size // 1024} KB)")
    if not files and not cron_installed():
        print("  nothing: no cron job and no saved data")
        return
    if not ask("Continue? (y/n)", "n").lower().startswith("y"):
        print("Nothing removed.")
        return
    uninstall_cron()
    for f in files:
        f.unlink()
    for d in {f.parent for f in files}:
        try:
            d.rmdir()  # only if empty
        except OSError:
            pass
    print(
        "Saved data removed. To remove the tool itself: "
        f"rm {shlex.quote(tilde(Path(__file__).resolve()))}"
    )


def parse_pmset(text):
    """Return (inputs, idle, calls, log_start).

    inputs: (time, first) moments of keyboard/mouse input. first=True when the
            moment is known to end an idle period (unlock, user wake, input
            after timeout); False when it is the last input seen by a sample.
    idle:   (start, end) stretches known to have no input.
    calls:  (start, end) meetings.
    """
    inputs, idle, calls = [], [], []
    asleep = display_off = call_start = log_start = None
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        stamp, kind, msg = m.groups()
        ts = local(datetime.strptime(stamp, STAMP_FMT))
        log_start = log_start or ts
        kind = kind.strip()
        if kind == "Assertions":
            a = ASSERT_RE.search(msg)
            if not a:
                continue
            proc, action, atype, name, h, mi, s = a.groups()
            age = timedelta(hours=int(h), minutes=int(mi), seconds=int(s))
            if proc == "WindowServer" and atype == "UserIsActive":
                last = ts - age
                if age:
                    idle.append((last, ts))
                if not any(d in name for d in IGNORE_DEVICES):
                    inputs.append((last, action in ("Created", "TurnedOn")))
            elif proc == "loginwindow" and atype == "UserIsActive" and action == "Created":
                inputs.append((ts, True))
            elif MEETING_RE.search(name) and atype == "NoDisplaySleepAssertion":
                if action == "Created":
                    call_start = ts
                elif action == "Released" and call_start:
                    calls.append((call_start, ts))
                    call_start = None
        elif kind == "Sleep":
            asleep = asleep or ts
        elif kind == "Wake":
            if asleep:
                idle.append((asleep, ts))
                asleep = None
            if "UserActivity" in msg or "HID Activity" in msg:
                inputs.append((ts, True))
        elif "Display is turned off" in msg:
            display_off = display_off or ts
        elif "Display is turned on" in msg and display_off:
            idle.append((display_off, ts))
            display_off = None
    return inputs, idle, calls, log_start


def current_idle():
    """Seconds since last keyboard/mouse input right now, or None."""
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True
        ).stdout
        return int(re.search(r'"HIDIdleTime" = (\d+)', out).group(1)) / 1e9
    except (OSError, AttributeError):
        return None


def merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def active_blocks(inputs, idle, calls, brk):
    """Group input moments into blocks, split where >= brk of known idle lies between."""
    idle = merge(idle)
    idle_ends = [e for _, e in idle]

    def idle_between(a, b):
        total, last_end = timedelta(), a
        i = bisect.bisect_right(idle_ends, a)
        while i < len(idle) and idle[i][0] < b:
            s, e = max(idle[i][0], a), min(idle[i][1], b)
            if e > s:
                total += e - s
                last_end = e
            i += 1
        return total, last_end

    events = sorted([(t, t, first) for t, first in inputs] + [(s, e, True) for s, e in calls])
    blocks = []
    for s, e, first in events:
        if blocks:
            b = blocks[-1]
            gap, known_end = idle_between(b[1], s)
            if s <= b[1] or gap < brk:
                b[1] = max(b[1], e)
                continue
            if not first and known_end < s:
                # Only a sample saw this input: the return happened somewhere
                # between the end of the known idle stretch and s.
                s = known_end + (s - known_end) / 2
        blocks.append([s, e])
    return blocks


# --- signals: (time, path or browser profile) --------------------------------------


def browser_profiles():
    """[(key, kind, history file)] for each browser profile found."""
    found = []

    def add(key, kind, path):
        if any(k == key for k, _, _ in found):
            key = f"{key} ({path.parent.name})"
        found.append((key, kind, path))

    for browser, sub in CHROMIUM.items():
        root = APP_SUPPORT / sub
        if not os.path.isdir(root):
            continue
        try:
            cache = json.loads((root / "Local State").read_text())["profile"]["info_cache"]
        except (OSError, ValueError, KeyError, TypeError):
            cache = {}
        for hist in sorted(root.glob("*/History")):
            info = cache.get(hist.parent.name, {})
            who = info.get("user_name") or info.get("name") or hist.parent.name
            add(f"{browser} {who}", "chromium", hist)
        if os.path.isfile(root / "History"):  # Opera keeps its profile at the top
            add(browser, "chromium", root / "History")
    for browser, sub in FIREFOX.items():
        for hist in sorted((APP_SUPPORT / sub / "Profiles").glob("*/places.sqlite")):
            try:
                account = json.loads((hist.parent / "signedInUser.json").read_text())
                who = account["accountData"]["email"]
            except (OSError, ValueError, KeyError, TypeError):
                who = hist.parent.name.split(".", 1)[-1]
            add(f"{browser} {who}", "firefox", hist)
    if os.path.isdir(HOME / "Library/Safari"):
        add("Safari", "safari", HOME / "Library/Safari/History.db")
    return found


def browser_visits(kind, path, since, quiet):
    """Local (not synced from other devices) visit times from one history file,
    and (start, end) of pages that were in front for most of their visit."""
    table, col, epoch, unit, sync_col, local_only = HISTORY[kind]
    floor = (since.astimezone(timezone.utc) - epoch) / unit
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / path.name
        try:
            # Browsers lock their history; copy it, with the WAL holding recent visits.
            shutil.copyfile(path, db)
            for suffix in ("-wal", "-journal"):
                if os.path.isfile(f"{path}{suffix}"):
                    shutil.copyfile(f"{path}{suffix}", f"{db}{suffix}")
        except OSError as e:
            if not quiet:
                hint = " (give your terminal app Full Disk Access)" if kind == "safari" else ""
                warn(f"history unreadable: {tilde(path)}: {e.strerror}{hint}")
            return [], []
        con = sqlite3.connect(db)
        try:
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            where = f"{col} >= ?" + (f" AND {local_only}" if sync_col in cols else "")
            rows = con.execute(f"SELECT {col} FROM {table} WHERE {where}", (floor,))
            times = [local(epoch + v * unit) for (v,) in rows]
            return times, in_front(con, kind, epoch, unit, floor)
        except sqlite3.Error as e:
            if not quiet:
                warn(f"history unreadable: {tilde(path)}: {e}")
            return [], []
        finally:
            con.close()


def in_front(con, kind, epoch, unit, floor):
    """(start, end) of pages in front for most of their visit, placed from the
    visit start; none if the browser doesn't record it."""
    try:
        rows = con.execute(IN_FRONT[kind], (floor,)).fetchall() if kind in IN_FRONT else []
    except sqlite3.Error:
        return []
    out = []
    for start, front, length in rows:
        if front >= 0.9 * length:
            s = local(epoch + start * unit)
            out.append((s, s + front * unit))
    return out


def editor_saves(since):
    """(time, file) of each save kept in VS Code-style local history."""
    floor = since.timestamp()
    out = []
    for app in EDITORS:
        for entries in (APP_SUPPORT / app / "User/History").glob("*/entries.json"):
            try:
                if entries.stat().st_mtime < floor:
                    continue
                data = json.loads(entries.read_text())
            except (OSError, ValueError):
                continue
            resource = data.get("resource", "")
            if not resource.startswith("file://"):
                continue
            path = unquote(urlparse(resource).path)
            for entry in data.get("entries", []):
                ts = entry.get("timestamp")
                if isinstance(ts, (int, float)) and ts / 1000 >= floor:
                    out.append((datetime.fromtimestamp(ts / 1000), path))
    return out


def dir_activity(root, since):
    """(time, folder) of file changes and git reflog entries under root."""
    if not os.access(root, os.R_OK | os.X_OK):
        warn(f"folder unreadable: {tilde(root)}")
        return []
    floor = since.timestamp()
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            try:
                with open(os.path.join(dirpath, ".git", "logs", "HEAD")) as f:
                    for line in f:
                        m = REFLOG_RE.search(line)
                        if m and int(m.group(1)) >= floor:
                            out.append((datetime.fromtimestamp(int(m.group(1))), dirpath))
            except OSError:
                pass
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            try:
                mtime = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            if mtime >= floor:
                out.append((datetime.fromtimestamp(mtime), dirpath))
    return out


def claude_prompts(since):
    """(time, folder) of each prompt typed in Claude Code."""
    try:
        fh = CLAUDE_HISTORY.open(errors="replace")
    except OSError as e:
        warn(f"Claude prompt history unreadable: {e}")
        return []
    out = []
    with fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            ts = entry.get("timestamp")
            if isinstance(ts, (int, float)) and ts / 1000 >= since.timestamp():
                out.append((datetime.fromtimestamp(ts / 1000), entry.get("project") or ""))
    return out


def claude_sessions(roots, since):
    """(time, folder) of Claude Code session messages started in roots or below."""
    floor = since.timestamp()
    out = []
    for root in roots:
        prefix = re.sub(r"[^A-Za-z0-9]", "-", str(root))
        for proj in CLAUDE_PROJECTS.glob(prefix + "*"):
            if proj.name != prefix and not proj.name.startswith(prefix + "-"):
                continue
            for f in proj.rglob("*.jsonl"):
                if f.stat().st_mtime >= floor:
                    out += session_lines(f, since, str(root), lambda e: e.get("cwd"))
    return out


def codex_sessions(since):
    """(time, folder) of Codex session events (CLI, IDE and app)."""

    def cwd_of(entry):
        payload = entry.get("payload")
        if entry.get("type") in ("session_meta", "turn_context") and isinstance(payload, dict):
            return payload.get("cwd")

    floor = since.timestamp()
    out = []
    for f in CODEX_HOME.glob("*sessions/**/*.jsonl"):  # sessions/ and archived_sessions/
        if f.stat().st_mtime >= floor:
            out += session_lines(f, since, "", cwd_of)
    return out


def session_lines(path, since, cwd, cwd_of):
    """(time, folder) of each timestamped line of a JSONL session log; the folder
    is the latest one cwd_of(line) gave."""
    out = []
    with path.open(errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                cwd = cwd_of(entry) or cwd
                t = iso_time(entry.get("timestamp"))
                if t and t >= since:
                    out.append((t, cwd))
    return out


def gemini_sessions(since):
    """(time, folder) of Gemini CLI chat messages; the folder is the project root."""
    try:
        names = json.loads((GEMINI_HOME / "projects.json").read_text())["projects"]
        roots = {name: path for path, name in names.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        roots = {}
    floor = since.timestamp()
    out = []
    for f in (GEMINI_HOME / "tmp").glob("*/chats/session-*.json"):
        try:
            if f.stat().st_mtime < floor:
                continue
            chat = json.loads(f.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        try:
            root = (f.parent.parent / ".project_root").read_text().strip()
        except OSError:  # older versions: only projects.json names the folder
            root = roots.get(f.parent.parent.name, "")
        for msg in chat.get("messages", []) if isinstance(chat, dict) else []:
            t = iso_time(msg.get("timestamp")) if isinstance(msg, dict) else None
            if t and t >= since:
                out.append((t, root))
    return out


def iso_time(text):
    """Local time from an ISO 8601 UTC stamp ("...Z"), or None."""
    try:
        return local(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except (AttributeError, ValueError):
        return None


class Projects:
    """Turns signals into project names using the config; the rest is other."""

    def __init__(self, config):
        self.names = list(config.get("projects", []))
        folders = config.get("folders", {})
        # Most specific folder first.
        self.folders = sorted(
            ((os.path.expanduser(f), p) for f, p in folders.items() if p in self.names),
            key=lambda fp: -len(fp[0]),
        )
        self.browsers = {
            k.lower(): p for k, p in config.get("browsers", {}).items() if p in self.names
        }
        meetings = config.get("meetings")
        self.meetings = meetings if meetings in self.names else "meetings"

    def of_path(self, path):
        for folder, project in self.folders:
            if path == folder or path.startswith(folder + "/"):
                return project
        return OTHER

    def of_browser(self, key):
        return self.browsers.get(key.lower(), OTHER)

    def roots(self):
        """Configured folders not inside another configured folder."""
        folders = sorted({f for f, _ in self.folders})
        return [f for f in folders if not any(f.startswith(g + "/") for g in folders)]


class Labeler:
    """Meetings during calls; else the label of the nearest signal within window;
    else of the browser page in front (latest started); else other."""

    def __init__(self, points, calls, window, meetings, spans=()):
        points.sort()
        self.times = [t for t, _ in points]
        self.labels = [lab for _, lab in points]
        self.calls = calls
        self.window = window
        self.meetings = meetings
        # A visible tab may sit beside the app in use, so spans only fill gaps.
        self.front = {}
        for s, e, lab in sorted(spans):
            m = s.replace(second=0, microsecond=0)
            while m < e:
                self.front[m] = lab
                m += timedelta(minutes=1)

    def __call__(self, t):
        if any(s <= t <= e for s, e in self.calls):
            return self.meetings
        i = bisect.bisect_left(self.times, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.times):
                d = abs(self.times[j] - t)
                if d <= self.window and (best is None or d < best[0]):
                    best = (d, self.labels[j])
        if best:
            return best[1]
        return self.front.get(t.replace(second=0, microsecond=0), OTHER)


def estimate_blocks(times, until, gap):
    """Blocks of signal times before `until` that lie within gap of each other.

    Used where the archive has no keyboard/mouse data.
    """
    blocks = []
    for t in sorted(times):
        if t >= until:
            break
        if blocks and t - blocks[-1][1] <= gap:
            blocks[-1][1] = t
        else:
            blocks.append([t, t])
    return blocks


# --- configuration ---------------------------------------------------------------


def tilde(path):
    path, home = str(path), str(HOME)
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


def top_folder(path):
    """~/name for a path inside the home folder (not Library or hidden), else None."""
    home = str(HOME) + "/"
    if not path.startswith(home):
        return None
    name = path[len(home) :].split("/", 1)[0]
    if not name or name.startswith(".") or name == "Library":
        return None
    return "~/" + name


def load_config(path):
    """Saved config, None before the first setup."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        warn(f"config unreadable ({e}); run --configure")
        return {}


def save_config(path, config):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
        return True
    except OSError as e:
        warn(f"config not saved: {e}")
        return False


def ask_history(config, config_path, archive):
    """Ask once whether to keep a history with the hourly cron job; save the answer."""
    if cron_installed():
        keep = ask("History: kept by the hourly cron job. Keep it? (y/n)", "y")
    else:
        print(
            "macOS keeps only ~7 days of keyboard/mouse data. To keep a history, an\n"
            "hourly cron job (about 1 s per run) saves it to\n"
            f"{tilde(archive)}: 1 year kept, at most ~12 MB.\n"
            "Without it, weeks older than 7 days are estimated (*)."
        )
        keep = ask("Keep a history? (y/n)", "y")
    config["keep_history"] = keep.lower().startswith("y")
    if config["keep_history"]:
        if not cron_installed():
            install_cron(archive)
    else:
        uninstall_cron()
    save_config(config_path, config)
    print()


def setting(config, key):
    return config.get("settings", {}).get(key, SETTINGS[key][0])


def edit_settings(config, path):
    """Ask for each number in SETTINGS; save only values that differ from the default."""
    saved = config.get("settings", {})
    print("Settings. Enter keeps [current]; - resets to the default.")
    changed = {}
    for key, (default, low, high, question) in SETTINGS.items():
        while True:
            answer = ask(
                f"  {question} ({low}-{high}, default {default})", str(saved.get(key, default))
            )
            if answer == "-":
                answer = str(default)
            if answer.isdigit() and low <= int(answer) <= high:
                break
            print(f"  answer a number from {low} to {high}")
        if int(answer) != default:
            changed[key] = int(answer)
    config["settings"] = changed
    if save_config(path, config):
        print(f"Saved {tilde(path)}")


def describe(config):
    names = config.get("projects", [])
    lines = []
    for name in names:
        items = [f for f, p in config.get("folders", {}).items() if p == name]
        items += [b for b, p in config.get("browsers", {}).items() if p == name]
        if config.get("meetings") == name:
            items.append("Teams calls")
        lines.append(f"  {name}: {', '.join(items) or '-'}")
    if config.get("meetings") not in names:
        lines.append("  meetings: Teams calls")
    return "\n".join(lines)


def ask(question, default):
    try:
        answer = input(f"{question} [{default}]: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def choose(question, names, current):
    """Project for one item by number or name; - for none; Enter keeps current."""
    default = str(names.index(current) + 1) if current in names else "-"
    while True:
        answer = ask(question, default)
        if answer == "-":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        if answer in names:
            return answer
        print(f"  answer 1-{len(names)}, a project name, or -")


def activity_counts():
    """Number of Claude Code prompts and editor saves per folder under ~, parents included."""
    home = str(HOME)
    dirs = [folder for _, folder in claude_prompts(EARLIEST)]
    dirs += [os.path.dirname(file) for _, file in editor_saves(EARLIEST)]
    counts = Counter()
    for d in dirs:
        while d.startswith(home + "/"):
            counts[d] += 1
            d = os.path.dirname(d)
    return counts


def ask_folders(names, folders, counts, menu):
    """Line-by-line folder questions, for when there is no terminal to browse in."""
    home = str(HOME)
    top = Counter(
        {tilde(d): n for d, n in counts.items() if os.path.dirname(d) == home and top_folder(d)}
    )
    print(f"\nFolders (Claude Code prompts + editor saves). Project: {menu}")
    chosen = {}
    for folder in [f for f, _ in top.most_common()] + [f for f in folders if f not in top]:
        project = choose(f"  {folder:<34} {top[folder]:>6}", names, folders.get(folder))
        if project:
            chosen[folder] = project
    print("More folders, e.g. a sub-folder for another project; empty line to finish.")
    while True:
        try:
            extra = input("  path: ").strip()
        except EOFError:
            extra = ""
        if not extra:
            return chosen
        folder = tilde(os.path.expanduser(extra))
        if not os.path.isdir(os.path.expanduser(extra)):
            print(f"  note: {folder} does not exist (yet)")
        project = choose(f"  {folder}", names, folders.get(folder))
        if project:
            chosen[folder] = project


def browse_folders(names, folders, counts):
    """Arrow-key browser of the folders under ~ to assign projects; returns {~/path: project}."""
    import curses
    import locale

    locale.setlocale(locale.LC_ALL, "")
    folders = dict(folders)
    home = str(HOME)

    def children(path):
        # A folder's contents are read only when opened, so macOS asks for
        # Desktop/Documents access only for folders the user opens.
        found = set()
        try:
            with os.scandir(path) as entries:
                for e in entries:
                    if (
                        e.is_dir(follow_symlinks=False)
                        and not e.name.startswith(".")
                        and e.name not in SKIP_DIRS
                        and not (path == home and e.name == "Library")
                    ):
                        found.add(e.path)
        except OSError:
            pass
        known = list(counts) + [os.path.expanduser(f) for f in folders]
        found.update(d for d in known if os.path.dirname(d) == path)
        return sorted(found, key=lambda d: (-counts[d], os.path.basename(d).lower()))

    def project_of(d):
        if tilde(d) in folders:
            return folders[tilde(d)], curses.A_BOLD
        while d != home and d != "/":
            d = os.path.dirname(d)
            if tilde(d) in folders:
                return f"({folders[tilde(d)]})", curses.A_DIM
        return "", curses.A_NORMAL

    def run(scr):
        curses.curs_set(0)
        keys = "  ".join(f"{n}={name}" for n, name in enumerate(names[:9], 1))
        utf8 = "utf" in locale.getpreferredencoding(False).lower()
        arrows = "↑↓ move  → open  ← back" if utf8 else "up/down move  right open  left back"
        path, pos, top, stack = home, 0, 0, []
        items = children(path)
        while True:
            h, w = scr.getmaxyx()
            rows = max(1, h - 4)
            pos = max(0, min(pos, len(items) - 1))
            top = min(max(top, pos - rows + 1), pos)
            scr.erase()
            scr.addnstr(0, 0, tilde(path) + "/", w - 1, curses.A_BOLD)
            scr.addnstr(1, 0, f"{arrows}  {keys}  - clear  Enter done", w - 1)
            if not items:
                scr.addnstr(3, 2, "(no subfolders)", w - 3, curses.A_DIM)
            for row, d in enumerate(items[top : top + rows]):
                project, attr = project_of(d)
                line = f" {os.path.basename(d):<40.40} {counts[d] or '':>6}  {project}"
                if top + row == pos:
                    attr |= curses.A_REVERSE
                scr.addnstr(3 + row, 0, line.ljust(w - 1), w - 1, attr)
            key = scr.getch()
            item = items[pos] if items else None
            if key in (curses.KEY_UP, ord("k")):
                pos -= 1
            elif key in (curses.KEY_DOWN, ord("j")):
                pos += 1
            elif key in (curses.KEY_RIGHT, ord("l")) and item:
                stack.append((path, pos, top))
                path, pos, top = item, 0, 0
                items = children(path)
            elif key in (curses.KEY_LEFT, ord("h"), curses.KEY_BACKSPACE, 127) and stack:
                path, pos, top = stack.pop()
                items = children(path)
            elif item and ord("1") <= key < ord("1") + min(len(names), 9):
                folders[tilde(item)] = names[key - ord("1")]
            elif item and key in (ord("-"), ord("0"), curses.KEY_DC):
                folders.pop(tilde(item), None)
            elif key in (curses.KEY_ENTER, 10, 13, ord("q")):
                return

    curses.wrapper(run)
    return folders


def configure(path, browsers, config, archive, first):
    """Ask for projects and what belongs to each; save and return the config."""
    config = dict(config or {})
    print(
        "Which work projects are there, and what belongs to each?\n"
        "Anything not assigned counts as 'other'. Enter keeps [current]; - = not work."
    )
    if config.get("projects"):
        print(f"\nCurrent settings ({tilde(path)}):\n{describe(config)}")

    names = []
    while not names:
        answer = ask("\nProjects, comma-separated", ", ".join(config.get("projects", [])) or "work")
        names = list(dict.fromkeys(n.strip() for n in answer.split(",") if n.strip()))
        reserved = [n for n in names if n.lower() in RESERVED]
        if reserved:
            print(f"  reserved names: {', '.join(reserved)}")
            names = []
    menu = "  ".join(f"{n}={name}" for n, name in enumerate(names, 1))

    old = config.get("folders", {})
    counts = activity_counts()
    folders = None
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            folders = browse_folders(names, old, counts)
        except Exception as e:  # no usable terminal for curses: ask line by line
            warn(f"folder browser unavailable ({e})")
    if folders is None:
        folders = ask_folders(names, old, counts, menu)
    config["folders"] = dict(sorted(folders.items()))
    print("\nFolders:")
    for folder, project in config["folders"].items():
        print(f"  {folder:<40} {project}")

    old = config.get("browsers", {})
    found = [k for k, _, _ in browsers]
    print(f"\nBrowser profiles. Project: {menu}")
    if not found:
        print("  none found")
    assigned = {}
    for key in found + [k for k in old if k not in found]:
        project = choose(f"  {key:<40}", names, old.get(key))
        if project:
            assigned[key] = project
    config["browsers"] = assigned

    config["meetings"] = choose(
        f"\nTeams calls count for (- = own 'meetings' row). {menu}", names, config.get("meetings")
    )
    config["projects"] = names
    print(f"\nSaved {tilde(path)}:" if save_config(path, config) else "\nUsing for this run:")
    print(describe(config) + "\n")
    if not first:  # the first run is only about projects
        ask_history(config, path, archive)
    return config


# --- output --------------------------------------------------------------------

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def round_to(dt, step):
    base = datetime.combine(dt.date(), time())
    return base + round((dt - base) / step) * step


def fmt(td):
    mins = int(td.total_seconds() // 60)
    return f"{mins // 60}h{mins % 60:02d}"


def hours(td):
    return f"{td.total_seconds() / 3600:.2f}"


def cell(text, est, width):
    """Right-aligned value; estimates get a * and, on a terminal, yellow."""
    text = (text + ("*" if est else " ")).rjust(width)
    return f"\033[33m{text}\033[0m" if est and COLOR else text


def day_rows(blocks, lo, label, step, min_len, est=False):
    """[start, end, label, est] runs of 5-min slots in the 24 h starting at lo."""
    hi = lo + timedelta(days=1)
    rows = []
    for s, e in blocks:
        s, e = max(s, lo), min(e, hi)
        if e - s < min_len:
            continue
        slot, end = round_to(s, step), round_to(e, step)
        while slot < end:
            lab = label(slot + step / 2)
            if rows and rows[-1][2] == lab and rows[-1][1] == slot:
                rows[-1][1] = slot + step
            else:
                rows.append([slot, slot + step, lab, est])
            slot += step
    return rows


def totals_of(rows):
    """label -> [total, estimated part]"""
    totals = {}
    for s, e, lab, est in rows:
        t = totals.setdefault(lab, [timedelta(), timedelta()])
        t[0] += e - s
        if est:
            t[1] += e - s
    return totals


def add_to(acc, totals):
    for lab, (total, est) in totals.items():
        t = acc.setdefault(lab, [timedelta(), timedelta()])
        t[0] += total
        t[1] += est


def sum_of(totals, labels, part=0):
    return sum((totals[k][part] for k in labels if k in totals), timedelta())


def summary(totals, work, other):
    total, est = sum_of(totals, work), sum_of(totals, work, 1)
    text = f"work {hours(total)}h" + (f" ({hours(est)}h estimated)" if est else "")
    if len(work) > 1:
        text += ": " + ", ".join(f"{k} {hours(sum_of(totals, [k]))}h" for k in work)
    if other:
        rest = sum_of(totals, [OTHER])
        text += f"  other {hours(rest)}h  total {hours(total + rest)}h"
    return text


def print_list(days, results, work, other):
    shown = work + ([OTHER] if other else [])
    grand = {}
    for day in days:
        print(day.strftime("%a %Y-%m-%d"))
        rows = [r for r in results[day] if r[2] in shown]
        if not rows:
            print("  -\n")
            continue
        for s, e, lab, est in rows:
            print(f"  {s:%H:%M} {cell(fmt(e - s), est, 7)}" + (f" {lab}" if len(shown) > 1 else ""))
        totals = totals_of(rows)
        add_to(grand, totals)
        print(f"  {summary(totals, work, other)}\n")
    print(f"{len(days)} days: {summary(grand, work, other)}")


def print_calendar(days, results, work, other):
    """Mon-Sun weeks covering days: first start and hours per project per day."""
    shown = work + ([OTHER] if other else [])
    keys = (
        ["start"] + shown + (["all work"] if len(work) > 1 else []) + (["total"] if other else [])
    )
    width, label_width = 9, max(8, max(len(k) for k in keys) + 1)

    def column(totals, start=None):
        def hours_cell(labels):
            total = sum_of(totals, labels)
            return cell(hours(total) if total else "-", bool(sum_of(totals, labels, 1)), width)

        col = {k: hours_cell([k]) for k in shown}
        col["all work"] = hours_cell(work)
        col["total"] = hours_cell(shown)
        col["start"] = (
            cell(f"{start[0]:%H:%M}", start[1], width)
            if start
            else cell("" if start is None else "-", False, width)
        )
        return col

    grand = {}
    monday = days[0] - timedelta(days=days[0].weekday())
    while monday <= days[-1]:
        week = [monday + timedelta(days=i) for i in range(7)]
        wk = {}
        cols = []
        for d in week:
            if d not in results:
                cols.append(dict.fromkeys(keys, " " * width))
                continue
            rows = [r for r in results[d] if r[2] in shown]
            totals = totals_of(rows)
            add_to(wk, totals)
            cols.append(column(totals, (rows[0][0], rows[0][3]) if rows else ()))
        add_to(grand, wk)
        week_col = column(wk)
        print(f"Week {monday.isocalendar()[1]:02d}  {week[0]} .. {week[-1]}")
        print(
            " " * label_width
            + "".join(f"{d:%a %d} ".rjust(width) for d in week)
            + "week ".rjust(width)
        )
        for k in keys:
            print(f"{k:<{label_width}}" + "".join(c[k] for c in cols) + week_col[k])
        print()
        monday += timedelta(days=7)
    print(f"{len(days)} days: {summary(grand, work, other)}")


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"timesheet {VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-w", "--weeks", type=int, help="show the last N weeks, Mon-Sun")
    p.add_argument("-n", "--days", type=int, help="show the last N days instead")
    p.add_argument("--list", action="store_true", help="one row per work period")
    p.add_argument("--other", action="store_true", help="also show time that is not work")
    p.add_argument(
        "--configure", action="store_true", help="projects, what belongs to them, history"
    )
    p.add_argument("--settings", action="store_true", help="day start, rounding and other numbers")
    p.add_argument(
        "--uninstall", action="store_true", help="remove the cron job and all saved data"
    )
    # Not for users: the cron job and the tests.
    p.add_argument("--collect", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--pmset-file", help=argparse.SUPPRESS)
    p.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help=argparse.SUPPRESS)
    args = p.parse_args()
    archive = args.archive.expanduser()
    config_path = DEFAULT_CONFIG

    if args.uninstall:
        return uninstall(config_path, archive)
    if args.collect:
        update_archive(archive, read_pmset(args.pmset_file))
        return
    if args.settings:
        return edit_settings(load_config(config_path) or {}, config_path)
    browsers = browser_profiles()
    config = load_config(config_path)
    first = config is None or "projects" not in config
    interactive = sys.stdin.isatty()
    if args.configure or (first and interactive):
        config = configure(config_path, browsers, config, archive, first)
        if args.configure:
            return
    elif first:
        warn("no projects set up yet: run with --configure")
        config = config or {}
    elif "keep_history" not in config and interactive:
        ask_history(config, config_path, archive)
    projects = Projects(config)

    text = update_archive(
        archive, read_pmset(args.pmset_file), save=bool(config.get("keep_history"))
    )
    step = timedelta(minutes=setting(config, "step"))
    window = timedelta(minutes=setting(config, "window"))
    min_len = timedelta(minutes=setting(config, "min_block"))
    day_start = timedelta(hours=setting(config, "day_start"))
    weeks = args.weeks or setting(config, "weeks")
    now = datetime.now()
    today = (now - day_start).date()
    if args.days:
        first_day = today - timedelta(days=args.days - 1)
    else:
        first_day = today - timedelta(days=today.weekday(), weeks=weeks - 1)
    days = [first_day + timedelta(days=i) for i in range((today - first_day).days + 1)]
    since = datetime.combine(first_day, time()) + day_start - window

    inputs, idle, calls, log_start = parse_pmset(text)
    if not args.pmset_file:
        secs = current_idle()
        if secs is not None:
            inputs.append((now - timedelta(seconds=secs), False))
    blocks = active_blocks(inputs, idle, calls, timedelta(minutes=setting(config, "break")))

    paths = claude_prompts(since) + editor_saves(since)
    paths += claude_sessions(projects.roots(), since) + codex_sessions(since)
    paths += gemini_sessions(since)
    for root in projects.roots():
        paths += dir_activity(root, since)
    points = [(t, projects.of_path(path)) for t, path in paths]
    spans = []
    for key, kind, path in browsers:
        project = projects.of_browser(key)
        times, front = browser_visits(kind, path, since, project == OTHER)
        points += [(t, project) for t in times]
        spans += [(s, e, project) for s, e in front]
    for key in config.get("browsers", {}):
        if key not in {k for k, _, _ in browsers}:
            warn(f"browser profile not found: {key}")
    label = Labeler(points, calls, window, projects.meetings, spans)
    estimated = estimate_blocks([t for t, _ in points], log_start or now, window)

    results = {}
    for day in days:
        lo = datetime.combine(day, time()) + day_start
        results[day] = sorted(
            day_rows(blocks, lo, label, step, min_len)
            + day_rows(estimated, lo, label, step, min_len, est=True)
        )

    work = list(projects.names)
    if projects.meetings not in work and any(
        r[2] == projects.meetings for rows in results.values() for r in rows
    ):
        work.append(projects.meetings)
    print(describe(config) if config.get("projects") else "no projects set up: run --configure")
    hour = setting(config, "day_start")
    print(f"day = {hour:02d}:00-{hour:02d}:00, hours in decimal")
    if log_start is None or log_start > datetime.combine(first_day, time()) + day_start:
        since_txt = f"{log_start:%Y-%m-%d %H:%M}" if log_start else "now"
        legend = f"* = estimated from activity traces, no keyboard/mouse data before {since_txt}"
        print(f"\033[33m{legend}\033[0m" if COLOR else legend)
    print()
    if args.list:
        print_list(days, results, work, args.other)
    else:
        print_calendar(days, results, work, args.other)


if __name__ == "__main__":
    main()
