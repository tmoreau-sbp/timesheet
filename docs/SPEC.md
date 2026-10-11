# timesheet — technical specification

| | |
|---|---|
| **Status** | Implemented (v1) |
| **Owner** | [@tmoreau-sbp](https://github.com/tmoreau-sbp) |
| **Updated** | 2026-10-30 |
| **Scope** | Design and rationale. Usage is in the [README](../README.md). |

## 1. Summary

`timesheet.py` reports work time per day from a Mac's own activity: start time and hours (5-min steps), split by project, as a Mon–Sun calendar or a list.

- **Active time** comes from keyboard/mouse activity in the macOS power log, not from "screen on".
- **Projects** come from where the activity happened: Claude Code, editors, project folders, browser profiles and Teams calls.

It is a single Python 3.9+ standard-library script, run by the user in a terminal.

## 2. Goals and non-goals

**Goals**
- Fill in a timesheet in minutes: per day, start time and hours per project.
- No setup beyond answering a few questions; no install, no pip, no background service unless the user asks for one.
- Correct about idle time: no counting of breaks, screen-on time, `caffeinate` or the wait before sleep.
- Nothing leaves the Mac.

**Non-goals**
- Exact to the minute. 5-min steps are enough.
- Tracking time per app or per window.
- Telling leisure from personal time. Everything that isn't work is `other`.
- Windows or Linux support. CI also runs the tests on Linux because they fake the macOS data.
- Uploading to a timesheet system.

## 3. Requirements

| # | Requirement | Status |
|---|---|---|
| R1 | Per day: start time and duration of each active period | done |
| R2 | 5-minute precision is enough | done |
| R3 | Last 7 days at least | done |
| R4 | Idle time before the display/system goes to sleep must not count | done |
| R5 | `caffeinate` must not make idle time look active | done |
| R6 | Only work is classified; the rest is `other`, with no leisure/personal split | done |
| R7 | Native macOS Python (`/usr/bin/python3`), no pip modules unless clear benefit | done |
| R8 | A day starts at 03:00: late-night work belongs to the evening before | done |
| R9 | Totals in decimal hours | done |
| R10 | Calendar view, weeks Mon–Sun, last 4 weeks by default | done |
| R11 | Days without keyboard/mouse data are estimated and marked `*` (and yellow) | done |
| R12 | Show work only; the rest on request (`--other`) | done |
| R13 | First run sets up what counts as work; `--configure` reviews and changes it | done |
| R14 | Work split over several projects, each with its own folders and browser profiles | done |
| R15 | Folders classified by the user during setup, sub-folders included; arrow keys, `→` opens a folder | done |
| R16 | Browsers besides Chrome (Edge, Opera, Firefox, Safari, …) | done |
| R17 | VS Code activity (file paths) as a signal | done |
| R18 | First run is setup only; from the second run, ask once about keeping a history (cron job, stored data) | done |
| R19 | One option removes the cron job and all saved data | done |
| R20 | Tunable numbers (day start, break, rounding, …) changed through their own option | done |
| R21 | Few options: keep the tool lean | done |
| R22 | Tests and CI (formatting, syntax) in the repo, with a CI badge | done |
| R23 | Security: `SECURITY.md`, CodeQL, every action pinned to a SHA, 10-day cool-off on new releases (pinact) | done |
| R24 | Releases that contain only the tool, with the version stamped in, triggered by a tag | done |
| R25 | Conventional Commits, enforced by a CI job | done |
| R26 | `CHANGELOG.md` and release notes written automatically from the commits | done |
| R27 | Time spent on a browser page counts for its profile's project, for users who work mostly in a browser | done |
| R28 | Other AI coding agents (Codex, Gemini CLI) count like Claude Code | done |
| R29 | Tell home, office and customer sites apart: the hourly job notes network, VPN, screens and USB devices (opt-in), and labels show per day and per row | done; to tune once there is data |
| R30 | No command can hang the tool: each has a timeout | done |
| R31 | A browser profile or Teams calls can be split over several projects by %, set with the arrow keys | done |
| R32 | A place can be linked to a project (or a split): work there with no other project signal counts for it | done |
| R33 | Projects busy at the same time (agents side by side) share the time evenly | done |

## 4. Findings

From one week of real power log on a managed MacBook, plus the activity sources below. Personal details are left out.

### 4.1 Environment

- **The display-sleep timeout isn't fixed.** Battery, AC and "currently in use" settings can differ (2, 10 and 120 min on the test Mac). The logged input timeouts follow whichever applies: mostly `00:10:00`, sometimes `02:00:00`.
- **Native Python.** `/usr/bin/python3` is a stub that runs the Command Line Tools Python 3.9. On a Mac without Command Line Tools, the first run prompts to install them.
  - The stub can hand over to the first `python3` on `PATH` (for example a pyenv Python). That's harmless: the script uses only the standard library and needs Python 3.9 or newer.
  - Under cron (minimal `PATH`) it runs Apple's 3.9.
- **`~/Library/LaunchAgents` can be root-owned** on managed (MDM) Macs, so writing a LaunchAgent fails with `Errno 13`. The history job therefore uses cron (5.7).

### 4.2 Power log (`pmset -g log`)

A week of log is about 23,500 lines (5.6 MB); macOS keeps about 7 days, so anything longer needs the tool's own archive (5.7).

| Event type | Share of lines |
|---|---|
| Assertions | 79% |
| Kernel Client Acks | 3% |
| Sleep / Wake / DarkWake | 3% |
| Display on/off notifications | < 1% |

**Display on/off does not show user activity.**
- With a 120-min display sleep, a break can count as up to 2 h of activity (R4).
- Many things keep the display on without the user: browser video playback, audio (`coreaudiod`), messaging apps, video calls (Teams, FaceTime) and `caffeinate`.
- Wakes `due to Notification` turn the display on with nobody at the laptop.

**User input is logged by WindowServer.** Each line is a `UserIsActive` assertion named `com.apple.iohideventsystem.queue.tickle … product:<device> eventType:<n>`. The trailing duration is the time since the last keyboard/mouse input.
- `Summary`, every 15 min: time since last input. Last input = line time − duration.
- `Created` / `TurnedOn`: first input after a timeout.
- `TimedOut`: no input for the timeout (10 min or 2 h, see 4.1).

Example, a lunch break:
```text
12:47:13 Summary UserIsActive [USB mouse] 00:01:05   -> last input 12:46:08
13:47:14 Summary UserIsActive [USB mouse] 01:01:05   -> still 12:46:08
13:54:31 loginwindow Created UserIsActive "Loginwindow User Activity"   -> back
```

Input devices:

| Device | eventType | Handling |
|---|---|---|
| built-in keyboard / trackpad | 3 (keyboard), 11 (digitizer) | input |
| USB mouse or trackball | 17 (pointer) | input |
| (null) | 29 (biometric, likely Touch ID), 3 | input |
| USB sound card (Sound Blaster) | 3 (volume keys) | **ignored** |

The sound card fires a key event about 1 s after wakes `due to Notification`, with nobody present.

**Return markers**
- `loginwindow Created UserIsActive "Loginwindow User Activity"` appears each time the user comes back after idle (about 10 times a day). It is treated as an unlock; this reading is inferred from timing, not documented.
- `Wake … due to UserActivity / HID Activity / pwrbtn` means a wake by the user. `due to Notification` does not.

**caffeinate** creates its own assertions under `PID …(caffeinate)`, including a `UserIsActive`. Reading only WindowServer's input entries ignores it (R5). powerd's `com.apple.powermanagement.lidopen` `UserIsActive` is ignored the same way.

**Meetings.** Teams logs `NoDisplaySleepAssertion "Microsoft Teams Call in progress"` with `Created` and `Released` lines; the `Released` line includes the call length. During a call the user may not type for a long time, so calls count as active and as work.

**Idle tail confirmed (R4).** One morning: unlock at 08:54, last input at 08:59, then `TimedOut 02:00:00` two hours later, display off and `Idle Sleep`. Display-based counting would report about 2 h 05; the tool reports 5 min.

**Manual check.** For one full workday, the timeline read from the raw log by hand matched the tool's output to the 5-min step.

### 4.3 Activity signals

**Claude Code prompt history** (`~/.claude/history.jsonl`)
- One line per typed prompt: `timestamp` (ms since epoch), `project` (working folder), `display` (the prompt text, never used).
- On the test Mac: about 8,000 prompts over 6 months, spread over 8 folders.
- Not deleted by `cleanupPeriodDays`, so it reaches about 6 months back; transcripts keep 30 days.
- A typed prompt proves the user was at the keyboard: the strongest signal before the archive starts.

**Claude Code transcripts** (`~/.claude/projects/<encoded folder>/**/*.jsonl`)
- Each `user` / `assistant` / `attachment` / `system` line has a top-level `timestamp` (ISO 8601 UTC, ms).
- 92% of timestamped lines carry `cwd` (in a sample of 4,300), so messages can be assigned to a project by folder.

**Browsers**

| Family | Browsers (folder under `~/Library/Application Support`) | Profile name | History | Visit time | Synced visits excluded by |
|---|---|---|---|---|---|
| Chromium | Chrome, Chrome Beta/Canary, Chromium, Edge, Brave, Vivaldi, Arc, Opera (profile at top level), Opera GX | `Local State` → `profile.info_cache[<dir>].user_name` (email), else `.name`, else dir | `<profile>/History`, `visits.visit_time` | µs since 1601-01-01 UTC | `originator_cache_guid` non-empty |
| Firefox | Firefox, Zen, LibreWolf, Floorp, Waterfox | email from `signedInUser.json`, else profile name | `Profiles/<id>.<name>/places.sqlite`, `moz_historyvisits.visit_date` | µs since 1970 | `source = 1` |
| Safari | Safari (`~/Library/Safari/History.db`) | `Safari` | `history_visits.visit_time` | s since 2001-01-01 | `origin <> 0` |

- On the test Mac: 8 Chrome profiles and Safari are found (checked on real data).
- Browsers lock their history file, so the tool reads a copy, with the `-wal` / `-journal` files that hold recent visits.
- The synced-visit filters apply only when the column exists. Without them, phone browsing would count as laptop time.
- Safari's folder is protected by macOS privacy controls (TCC); reading it needs Full Disk Access for the terminal app.
- Safari 17 profiles are not read; only the default history is.
- **Time in front.** Chromium and Firefox also record how long each page was in front:
  - Chromium: `context_annotations.total_foreground_duration` (µs, joined on `visits.id`), the time the tab was visible; `-1000000` means not recorded. `visits.visit_duration` is no use: it counts background tabs (2,498 h in 14 days on the test Mac).
  - Firefox: `moz_places_metadata.total_view_time` (ms) from `created_at` (ms since 1970), active viewing only; on by default (`browser.places.interactions.enabled`).
  - Safari records no such time.
  - Only the total is stored, not when. On the test Mac's main work profile, 14 days held 884 pages with time in front, 88 h in all. Most were in front for their whole visit, but 22 tabs were in front for over an hour, some on and off. The tool keeps pages in front for at least 90% of their visit and places that time from the visit start.

**Codex** (`$CODEX_HOME`, default `~/.codex`; CLI, IDE extension and app share it)
- One JSONL per session: `sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`, moved to `archived_sessions/` when archived.
- Every line has `timestamp` (ISO 8601 UTC, ms), `type` and `payload`. `session_meta` and `turn_context` lines carry the working folder in `payload.cwd`.
- `exec_command_end` lines also have a `cwd`, but it is where one command ran (often `/tmp`), so it is ignored.
- `history.jsonl` holds prompts with `ts` but no folder, so it isn't used.

**Gemini CLI** (`~/.gemini`)
- One JSON per chat: `tmp/<project>/chats/session-<time>-<id>.json`, with `messages[].timestamp` (ISO 8601 UTC, ms).
- The project folder is in `tmp/<project>/.project_root`; older versions only map it in `projects.json` (`{"projects": {<folder>: <project>}}`).

**VS Code local history**
- `~/Library/Application Support/<editor>/User/History/<hash>/entries.json`, one file per saved file:
  ```json
  {"resource": "file:///Users/…/main.tf", "entries": [{"id": "…", "timestamp": 1760000000000}]}
  ```
- One entry per save, with the file path, so a save can be assigned to a project by folder (R17).
- Up to 50 saves per file are kept by default (`workbench.localHistory.maxFileEntries`); there is no time-based clean-up.
- Code, Code - Insiders, Cursor, VSCodium and Windsurf use the same layout.
- Files edited over Remote SSH never show up: VS Code keeps their history on the remote host (`workingCopyHistoryService.ts`: "Prefer history to be stored in the remote").

**Project folders**
- File modification times, skipping `.git`, `.terraform`, `node_modules`, venvs and caches.
- git reflog entries in `<repo>/.git/logs/HEAD` (unix timestamp per checkout, commit or pull).

### 4.4 Where the Mac is

Checked on macOS 26 for R29 (home, office or customer):
- **The Wi-Fi name is hidden.** `ipconfig getsummary` and `system_profiler` print `<redacted>` without Location Services, and `networksetup -getairportnetwork` says "not associated". `/var/log/wifi.log` is `root:admin`; `/Library/Preferences/com.apple.wifi.known-networks.plist` is root-only and keeps only the last join per network. So the name isn't used.
- **The DHCP answer is readable:** `ipconfig getpacket <interface>` gives the router address and the network's domain name, enough to tell most networks apart. `arp -n <router>` adds the router's hardware address, which separates the many home routers at `192.168.1.1`.
- **VPN.** The system's own `utun` interfaces carry only IPv6 link-local addresses. A VPN adds an IPv4 address on a `utun` interface and comes first in `scutil --nwi`, so the main network is the first non-VPN interface there. Cisco Secure Client, Ivanti Secure Access and WireGuard log to the unified log, which keeps no readable history; WireGuard also shows in `scutil --nc list`.
- **Screens:** `ioreg -l -r -k DisplayAttributes` gives the name and serial of external screens; the built-in screen has no name.
- **USB:** `ioreg -p IOUSB -l` gives vendor, name, vendor and product ID, and a serial when the device has one. `system_profiler SPUSBDataType` prints nothing on macOS 26. A screen's USB hub can report the screen's serial.
- **The power log names input devices only** (`UserIsActive … product:`), with an ID that changes at every plug-in or wake. Two units of a model without a serial look the same everywhere.
- **None of these keep a history,** so the cron job has to note them as it goes.

| Probe | Time per run |
|---|---|
| `scutil --nwi`, `ipconfig getsummary` / `getpacket`, `arp` | ~0.03 s |
| `ifconfig`, `scutil --nc list` | ~0.03 s |
| `ioreg` screens (`system_profiler`: 0.3 s) | 0.05 s |
| `ioreg` USB | 0.01 s |
| all | ~0.1 s, next to ~1 s for `pmset -g log` |

## 5. Design

### 5.1 Runtime

- `#!/usr/bin/python3`, written for Python 3.9 (the Command Line Tools version).
- Standard library only: `argparse bisect collections curses datetime hashlib json locale os pathlib re shlex shutil sqlite3 subprocess tempfile urllib.parse`.
- No pip module: SQLite, JSON and curses are in the standard library and the log is plain text, so a package brings no clear benefit (R7). Development tools (ruff) are used in CI only.
- Runs as the normal user; `pmset -g log` and the place probes (5.16) need no root.

### 5.2 Pipeline

```text
pmset -g log ──> archive (if history on) ──> parse ──> input moments, known-idle stretches, calls
                                               │
                                               ├──> active blocks (merge, split on breaks)
ioreg HIDIdleTime ──> "now" moment             │
                                               │
AI agents, editors, folders, browsers ──> (time, path or profile) ──> project ──> 5-min slots
                                               │
                                               └──> days 03:00–03:00 ──> calendar or list
```

### 5.3 Parsing

Line format: `YYYY-MM-DD HH:MM:SS ±zzzz <Type padded><TAB or 2+ spaces><message>`. Timestamps are converted to local time using the offset in each line, so DST is handled.

| Log entry | Produces |
|---|---|
| WindowServer `UserIsActive` (any action), device not ignored | input moment at line time − duration; `first` if the action is `Created`/`TurnedOn` |
| WindowServer `UserIsActive`, duration > 0 | known-idle stretch (line time − duration, line time) |
| loginwindow `Created UserIsActive` | input moment, `first` |
| `Wake` with `UserActivity` / `HID Activity` | input moment, `first` |
| `Sleep` … next full `Wake` | known-idle stretch (DarkWake doesn't end it) |
| Display off … display on | known-idle stretch |
| Teams `NoDisplaySleepAssertion` Created … Released | call interval |

### 5.4 Active blocks

1. Sort input moments and call intervals together.
2. Walk them in order. Join each moment to the current block unless the known-idle time between them is at least `break` (10 min). Counting known-idle time, rather than the raw gap, keeps continuous work together even though samples are 15 min apart.
3. A block ends at its last input moment, so the idle tail before display sleep or system sleep is excluded (R4).
4. If a block starts with a sample-only moment (not `first`), the user came back somewhere between the end of the known-idle stretch and that moment. The start is set to the midpoint, which caps the error at about 7.5 min.
5. On a live run, the current idle time from `ioreg` adds a final moment, so an ongoing session counts up to now.

### 5.5 Projects and labels (R6, R14, R15)

| Signal | Gives | Project from |
|---|---|---|
| Claude Code prompt | time, folder | folder |
| Claude Code session message (under configured folders) | time, `cwd` | folder |
| Codex session event | time, session `cwd` | folder |
| Gemini CLI chat message | time, project folder | folder |
| Editor save | time, file | folder |
| File change / git reflog (under configured folders) | time, folder | folder |
| Browser visit | time, profile | profile |
| Browser page in front | interval, profile | profile |
| Teams call | interval | `meetings` setting |

- **Folders:** the most specific configured folder wins, so `~/a/b` → project B beats `~/a` → project A. A path outside every configured folder is `other`.
- **Browser profiles:** a profile not assigned to a project is `other`.
- **Splits (R31):** a browser profile, Teams calls or a place (R32) can get several projects with a % each (`{"A": 70, "B": 30}`). Their slots carry the split as one label; totals give each project its % of the time, and `--list` shows the row as `A 70% + B 30%`. A project removed later drops out and the others are rescaled to 100%.
- **Slots:** block start and end are snapped to the `step` grid (5 min) and the block is cut into slots.
  - A slot inside a Teams call gets the meetings project, or a `meetings` row if none is set. Calls always win.
  - Otherwise, when signals with different labels lie within `overlap` (5 min) of the slot's middle, the slot is shared evenly between those labels (R33): two get 50% each, three 33%. Every label counts once, however many signals it has: a project, `other`, or a split (a 50/50 profile beside a folder gives 75/25). `0` turns it off.
  - Otherwise the slot takes the project of the nearest signal within `window` (15 min).
  - Failing that, it takes the browser page in front at that minute (the latest started if several). A visible tab may sit on a second screen beside the app in use, so this time only fills gaps and never beats a signal.
  - A slot still `other`, or the `other` share of a shared slot, takes the project of the place the Mac is at (R32, 5.16), if that place has one. Folders, profiles and calls always win, so work for project B at customer A stays B.
  - `other` signals are never configured. They only stop non-work activity next to work from counting as work.
- Consecutive slots with the same label form one row.

### 5.6 Estimates (R11)

Before the archive starts (`log_start`) there is no keyboard/mouse data.
- Signal times are grouped into blocks: consecutive signals no more than `window` apart form one block. All signals count, work and other.
- Browser time in front is left out: on a measured week it raised the estimate from 99.5% to 115% of measured time, since a visible tab isn't proof of use.
- Blocks are clipped at `log_start`, then go through the same slots, labels and `min_block` filter as measured blocks.
- A day or week containing estimated time shows `*` on its start and hours. On a terminal (and without `NO_COLOR`) these values are yellow.

Calibration: one measured week re-run as if it had no keyboard/mouse data, using Claude Code signals only. The estimate reached 96.5% of the measured week, each day within 1.7 h, and start times within 5 min except one day whose first activity was a Teams call, which only the power log shows.

### 5.7 History archive and cron job (R18)

**Consent.** The first run only sets up projects. On the second interactive run, or in `--configure` after the first, the tool asks once:
```text
History
  macOS keeps only ~7 days of keyboard/mouse data. An hourly cron job
  (about 1 s a run) can keep a year of it, at most ~12 MB, in
  ~/Library/Application Support/timesheet/pmset-archive.log
  Without it, weeks older than 7 days are estimated (*).
  Keep a history? (y/n) [y]: y
  Hourly cron job installed.
```
- The answer is stored as `keep_history`.
- If a cron job already exists, the section says `Kept by the hourly cron job in` the archive path and asks `Keep it? (y/n) [y]`.
- Saying no removes the cron job (`Cron job removed.`) and the places log.
- Each question is a section: a title, then its text and question indented by two spaces, with paths on a line of their own. Places (5.16) follow after a blank line.

**Archive.**
- **What is saved:** only when `keep_history` is on. Each run and each cron run merges the power-log lines that parsing (5.3) uses into the archive. Otherwise the archive is only read.
- **No duplicates.** An event is identified by its time in UTC plus its message with spacing collapsed. A line seen again in the next hour's log is skipped, and so is the same event printed in another time zone (travel) or with other column padding (macOS update).
- **Order.** The sort is stable, so the log's own order within one second is kept.
- **Writes.** The file is written through a temp file and a rename, and only when new lines arrived.
- **Retention:** 1 year (`ARCHIVE_DAYS = 366`), so the archive tops out at about 12 MB.

| Period | Size |
|---|---|
| day | 32 KB |
| week | 227 KB |
| month | ~1 MB |
| year (maximum) | ~12 MB |

**Cron job.**
```text
0 * * * * /usr/bin/python3 '<script>' --collect 2>'<data dir>/collect.log' # timesheet: save power log
```
- The tool's line is found by its tag or by `timesheet.py --collect`, so a line added by hand is also found. Installing replaces it; other crontab lines are kept.
- Paths are shell-quoted, because `Application Support` contains a space.
- cron rather than a LaunchAgent: it needs no write access to `~/Library/LaunchAgents` and works the same on any Mac.
- cron skips hours the Mac sleeps through. With hourly runs and ~7 days of log, nothing is lost.
- `collect.log` holds only the last run's errors.
- **Cost per run:** 0.26 s at 1 week of archive and 0.57 s at 1 year, plus 1–2 s for `pmset -g log`. That is about 0.05% of one core.

### 5.8 Setup: `--configure` (R13–R15)

`~/Library/Application Support/timesheet/config.json`:
```json
{
  "projects": ["ClientA", "Internal"],
  "folders": {"~/clients/a": "ClientA", "~/internal": "Internal"},
  "browsers": {"Chrome me@client-a.com": "ClientA", "Chrome me@company.com": {"ClientA": 70, "Internal": 30}},
  "meetings": "ClientA",
  "keep_history": true,
  "keep_places": true,
  "places": {"client-a": [
    {"network": "Wi-Fi corp.example 10.0.0.1 #1a2b3c4d", "screens": ["DELL U2723QE #5e6f7a8b"]},
    {"network": "Wi-Fi 172.20.10.1 #9f8e7d6c", "screens": ["DELL U2723QE #5e6f7a8b"]}
  ]},
  "place_projects": {"client-a": "ClientA"},
  "settings": {"day_start": 4}
}
```

**When setup runs.** On the first interactive run, setup runs and the report follows. `--configure` runs it again at any time, showing the current values first.

**What it asks:**
1. **Projects**, comma-separated (default `work`). `start`, `place`, `other`, `total`, `meetings` and `all work` are reserved.
2. **Folders**, in an arrow-key browser of `~` (curses):

   | Key | Action |
   |---|---|
   | `↑` `↓` | move |
   | `→` | open the folder |
   | `←` | back |
   | `1`–`9` | assign a project |
   | `-` | clear |
   | Enter | done |

   - Folders are sorted by activity (Claude prompts + editor saves, counted for the folder and its parents).
   - A folder without its own project shows its parent's project, dimmed.
   - A folder's contents are read only when it is opened, so macOS asks for Desktop/Documents access only for folders the user opens.
   - Without a terminal (piped input), or if curses fails, the questions are asked line by line instead.
3. **Browser profiles:** each profile found gets a project number, several (`1,2`), or `-`. Several are then split with the arrow keys:
   - It starts even (50/50; 50/25/25 for three) or at the current split for the same projects.
   - `←` `→` take 25% from or give 25% to the marked project, the difference going to or coming from the next project that can spare it. Every project keeps more than 0%; a refused step beeps.
   - `↑` `↓` mark another project (shown from three on); Enter keeps the split.
   - Without a terminal, or with more than four projects (25% steps), a % is typed for each but the last, which gets the rest.
4. **Teams calls:** a project number, several split as for profiles, or `-` for their own `meetings` row.
5. **Places** (5.16): a label per setup (network + screens) noted, only when there is a places log. Then a project per label, one or several as for profiles, or `-` for none.
6. **History** (5.7), then whether to note places (5.16): only when this isn't the first run.

**Answering.** Each question shows its current value in brackets. Enter keeps it, `-` means not work, and a project name works as well as its number.

**No config and no terminal** (cron, pipes): the tool warns `no projects set up yet` and counts only Teams calls as work.

### 5.9 Settings: `--settings` (R20)

| Name | Default | Range | Meaning |
|---|---|---|---|
| `weeks` | 4 | 1–104 | weeks shown (overridden by `-w`) |
| `day_start` | 3 | 0–23 | hour a day starts; earlier work counts for the day before |
| `break` | 10 | 1–120 | minutes without input that end a work block |
| `min_block` | 5 | 0–120 | shortest block kept, in minutes |
| `step` | 5 | 1–60 | rounding step, in minutes |
| `window` | 15 | 1–120 | minutes an activity signal covers around it; also the gap that splits estimated blocks |
| `overlap` | 5 | 0–60 | minutes within which signals of several labels share a slot evenly; 0 = off |

- Each number is asked in turn. Enter keeps the current value, `-` resets to the default, and out-of-range answers are asked again.
- Only values that differ from the default are stored (`config.json` → `settings`), so better defaults in a later version still apply.

### 5.10 Output

- A day runs from `day_start` to `day_start` the next day (R8). Hours are decimal (R9).
- One row per project, plus `all work` when there is more than one project. `--other` adds `other` and `total` (R12).
- The header lists what belongs to each project.

Calendar (default, last `weeks` weeks, Mon–Sun, this week included):
```text
  ClientA: ~/clients/a, Teams calls
  Internal: ~/internal
day = 03:00-03:00, hours in decimal
* = estimated from activity traces, no keyboard/mouse data before 2026-10-04 11:50

Week 41  2026-10-05 .. 2026-10-11
           Mon 05   Tue 06   Wed 07   Thu 08   Fri 09   Sat 10   Sun 11     week
start       08:30    08:00    08:15    08:45    09:00        -        -
ClientA      6.25     5.50     7.00     4.75     6.00        -        -    29.50
Internal     1.50     2.25     0.75     3.00     1.25        -        -     8.75
all work     7.75     7.75     7.75     7.75     7.25        -        -    38.25
```

| Cell | Meaning |
|---|---|
| `start` | first shown start of the day |
| `*` | contains estimated time |
| `-` | zero |
| empty | in the future |

List (`--list`), with a label column when more than one row type is shown:
```text
Sun 2026-10-11
  08:55   0h05  other
  11:05   0h30  Internal
  work 0.50h: ClientA 0.00h, Internal 0.50h  other 0.08h  total 0.58h
```

With place labels (5.16), a `place` row follows `start` in the calendar: the label with most of the day's time, cut to 8 characters. In the list, each row ends with `@label`, the place at its start.

### 5.11 Command line (R21)

| Option | Meaning |
|---|---|
| `-w N`, `--weeks N` | show the last N weeks, Mon–Sun (default: `weeks` setting) |
| `-n N`, `--days N` | show the last N days instead |
| `--list` | one row per work period instead of the calendar |
| `--other` | also show time that is not work |
| `--configure` | projects, what belongs to them, history |
| `--settings` | day start, rounding and other numbers |
| `--uninstall` | remove the cron job and all saved data |

Hidden from `--help`, not for users:

| Option | Used by |
|---|---|
| `--collect` | the cron job |
| `--pmset-file FILE` | the tests: read a saved log instead of running `pmset` |
| `--archive FILE` | the tests, and cron lines installed by older versions |

Removed to keep the tool lean: `--day-start`, `--break`, `--min`, `--step`, `--window` (now in `--settings`), `--config`, `--install` (`--configure` handles history), `--browsers` (setup lists the profiles), and the `--leisure` and `--profiles` aliases.

### 5.12 Uninstall (R19)

`--uninstall` lists what it will remove, with sizes, and asks `Continue? (y/n) [n]`:
- the cron job (other crontab lines are kept)
- `config.json`, `pmset-archive.log`, `places.log`, any leftover `.tmp` and `collect.log`
- the data folder, if it is then empty

It then prints the command that removes the tool file itself (`rm <path to timesheet.py>`), never a folder: a downloaded script may sit in `~/Downloads`. It never deletes anything it didn't create.

### 5.13 Releases (R24)

**Trigger.** Pushing a tag `vX.Y.Z` on `main` starts `.github/workflows/release.yml`.

**Steps:**
1. Run the test suite.
2. Stamp the version: `VERSION = "dev"` becomes `VERSION = "X.Y.Z"`. The version shows at the end of `--help` (`timesheet X.Y.Z`), so no `--version` option is needed. A test makes sure the line exists exactly once.
3. Sign `timesheet.py` with `actions/attest-build-provenance`, GitHub's own action. It writes a Sigstore-signed SLSA build provenance, stored by GitHub and returned as a bundle.
4. Write the release notes and the full `CHANGELOG.md` with git-cliff (5.14). GitHub's "Full changelog" link is appended to the notes.
5. Create the release with `gh release create --verify-tag --notes-file notes.md --generate-notes`. The `gh` CLI is already on GitHub's runners, so no third-party release action is needed.
6. Commit the regenerated `CHANGELOG.md` to `main` as `docs(changelog): vX.Y.Z`. The commit goes through the GitHub contents API, so GitHub signs it ("Verified") and no git credentials are kept in the workspace. Pushes made with the workflow token don't start other workflows, so this can't loop.

**Release files:**

| File | What |
|---|---|
| `timesheet.py` | the tool, executable, with the version stamped in |
| `timesheet.py.sigstore.json` | the signed attestation bundle |
| `timesheet.py.sha256` | checksum |

**Source archives.** GitHub's automatic "Source code" downloads can't be removed. `.gitattributes` `export-ignore` trims them to `timesheet.py`, `README.md` and `LICENSE`; clones are unaffected.

**Install from a release:**
```sh
curl -fLO https://github.com/tmoreau-sbp/timesheet/releases/latest/download/timesheet.py
chmod +x timesheet.py
gh attestation verify timesheet.py -R tmoreau-sbp/timesheet   # optional
```

**Permissions.** The release job only gets `contents: write` (release + changelog commit), `id-token: write` and `attestations: write`. The tag name reaches shell steps through `env:`, never inline, so it can't inject commands.

**Scorecard.** A signature among the release files scores 8/10 on Signed-Releases. 10/10 would need an `*.intoto.jsonl` file from the SLSA generator, which must be referenced by tag rather than SHA and so conflicts with the pinact rule.

**History.** Once a version is released, `main` is no longer rewritten; new work goes in as normal commits.

### 5.14 Commits and changelog (R25, R26)

**Format.** Commit messages follow Conventional Commits: `type(scope)!: description`. The types are `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`, `style` and `revert`; `!` or `BREAKING CHANGE:` marks a breaking change.

**git-cliff** (`cliff.toml`, version 2.14.2) parses them.
- `require_conventional = true` makes it fail on any other message.
- Merge commits (`Merge …`) are skipped.
- Commits are grouped as Features, Fixes, Performance, Refactoring, Documentation, Tests, Build and CI, and Other, and breaking changes are marked.

**Enforcement.**
- **CI job `commits`:** runs `git cliff --unreleased`, so every commit since the last release tag is checked, on each push and pull request. For pull requests it checks out the PR head, not GitHub's merge commit.
- **Local hook (optional, in CONTRIBUTING):** a `commit-msg` hook with the same rule, so mistakes are caught before pushing.
- **Dependabot:** writes `ci: bump …` messages (`commit-message.prefix: ci`).

**Changelog.**
- Each tag regenerates the whole `CHANGELOG.md` (newest version first, with dates) and the release notes for that version. Both come from the same `cliff.toml`, and the release workflow commits the file to `main` (5.13).
- It is never edited by hand.
- `git cliff --bumped-version` shows the version the commits call for: `feat` raises the minor number, `fix` the patch number, `!` the major.

**Tested locally with git-cliff 2.14.2:**
- conventional commits render in their groups
- a merge commit is skipped
- `Add stuff` fails with `UnconventionalCommitsError`
- the CONTRIBUTING hook accepts the examples and Dependabot's messages, and rejects `Add stuff`, `feat:missing space` and `Feat: capital`

### 5.15 Errors

- An unreadable source is skipped and the run continues:
  - a configured folder: warning
  - a browser profile assigned to a project: warning; for Safari, the warning says to grant Full Disk Access
  - an unassigned (`other`) profile: no warning
- A configured browser profile that no longer exists gets a warning.
- The tool exits only if the power log itself can't be read.
- **Timeouts (R30).** Every command the tool runs has one, so a run can't hang:

  | Command | Timeout | Then |
  |---|---|---|
  | `pmset -g log` | 60 s | exit with the error |
  | `crontab -l`, `crontab -` | 60 s: a macOS permission prompt may be waiting | exit with the error. Reading never falls back to an empty table, which would drop the user's own lines when written back |
  | `ioreg` idle time | 5 s | no "now" moment |
  | place probes | 5 s each | that part stays empty |

  Browser databases are read from private copies, so SQLite never waits on a lock. Only setup questions wait, for the user.
- If `crontab` fails (for example when macOS privacy controls block it), the tool exits with the error and two options: give the terminal app Full Disk Access, or run the tool at least every 6 days.

### 5.16 Places (R29)

**Consent.** Asked right after the history question (5.7), and once on the next interactive run by those who already keep a history:
```text
Places
  The hourly job can also note where you work, to tell home, office and
  customers apart: network (domain, router), VPN, screens and USB devices.
  A line only when they change, under 1 MB a year, in
  ~/Library/Application Support/timesheet/places.log
  Names only; serial numbers and hardware addresses hashed.
  Label them later with --configure.
  Note where you work? (y/n) [n]:
```
- The answer is stored as `keep_places`. Without a history there is no cron job, so places are off too.
- Saying no deletes `places.log`.

**Record.** Each `--collect` run probes (4.4) and builds:
```json
{"network": {"type": "Wi-Fi", "domain": "corp.example", "router": "10.0.0.1", "id": "1a2b3c4d"},
 "vpn": ["10.200.x.x"], "screens": ["LG ULTRAFINE #912d16fe"],
 "usb": ["Logitech USB Trackball 046d:c408"]}
```
- `id` and `#…` are the first 8 hex digits of a SHA-256 of the router's hardware address or of the device serial.
- A VPN is kept as the first two numbers of its address, or as its service name.
- Lists are sorted, so the same place gives the same record.
- Commands run by absolute path: cron's `PATH` has neither `/usr/sbin` nor `/sbin`.

**Log.** `places.log`, next to the archive, one line per record: `<local time with offset><TAB><JSON>`.
- A line is added only when the record differs from the last one, so a record holds until the next. About 400 bytes a line and a few lines a day: under 1 MB a year.
- Lines older than a year (`ARCHIVE_DAYS`) are dropped when a line is added.
- Written through `places.tmp` and a rename, mode `600`.
- Only the cron job writes it; a report never probes.

**Labels.** A place is told by its *setup*: the network and the screens. USB devices come and go (a stick, a phone), so they only help guess (below). `--configure` lists each setup in the log, grouped by network, with the dates it was seen, and asks for a label (home, office, a customer):
```text
  Wi-Fi corp.example 10.0.0.1 #1a2b3c4d
    DELL U2723QE #5e6f7a8b                       2026-10-20..10-20 [-]: client-a
    (no screen)                                  2026-10-20..10-21 [-]: client-a
  Wi-Fi 172.20.10.1 #9f8e7d6c
    DELL U2723QE #5e6f7a8b                       2026-10-21..10-22 [-]: client-a
    LG ULTRAFINE #912d16fe                       2026-10-22..10-22 [-]: home
```
- Several setups can be the same place: they get the same label. Stored in `config.json` → `places` as `{label: [setup, ...]}`.
- A phone hotspot can be at a customer and at home, so its setups can get different labels.

**Projects (R32).** Each label then gets a project, a split (as for profiles, 5.8) or none, stored in `place_projects`:
```text
Work at a place with no folder, profile or call: which project? 1=ClientA  2=Internal
  client-a             [-]: 1
  home                 [-]: -
```
- Active time at that place with no project signal (`other`) counts for the project. Signals still win (5.5).
- The header lists the place with its project: `ClientA: ~/clients/a, @client-a`.
- A label no longer given, or a project removed, drops out.

**Use.** The place at a time is that of the record then in force:
- A labeled setup gives its label.
- A setup not labeled (a new desk, a new screen) is guessed from the records of labeled setups: the one label its network had, else the one label its screens had, else its USB devices. Several labels at a step pass on to the next; none at the end means no place.
- The VPN isn't used yet. Reports show places only once there are labels (5.10).

## 6. Security and privacy

- **Local only.** No network access; nothing leaves the Mac.
- **Minimal reading.**
  - Browsers: only visit times and time in front are read, never URLs or titles.
  - AI agents (Claude Code, Codex, Gemini CLI): only times and folders, never prompt text.
  - Editors and folders: only times and paths, never file contents.
- **Stored data:** `config.json` (projects, folders, profile names, settings) and, with consent, the power-log archive (input times, unlocks, sleep/wake, Teams call times). Both stay in `~/Library/Application Support/timesheet/`, are capped (1 year, ~12 MB) and are removed by `--uninstall`.
- **Places** (opt-in, 5.16): names of networks, screens and USB devices, the router address and the network's domain. Serial numbers and hardware addresses are kept only as a short hash: it tells devices apart, but isn't secret, since anyone with the device can compute it. Never the Wi-Fi name, never a location.
- **No elevated rights:** no root, no LaunchAgent, no changes outside the data folder and the user's own crontab line.
- **Supply chain:**
  - The tool has no dependencies.
  - Every GitHub Action is pinned to a full commit SHA with a version comment.
  - Workflows get only the permissions they need (`permissions: {}` or `contents: read` by default) and check out with `persist-credentials: false`.
  - A pinact job in CI fails if an action isn't pinned, if its version comment doesn't match the SHA, or if the release is younger than 10 days (`.pinact.yml`).
  - Dependabot proposes updates weekly with the same 10-day cooldown, so a compromised release has time to be caught before it is used.
  - CodeQL scans the Python code and the workflows.
  - OpenSSF Scorecard rates the repo's practices weekly and on each push to `main`; its results go to the Security tab and the README badge.
- **Reporting:** `SECURITY.md` points to GitHub's private vulnerability reporting, which is enabled.

## 7. Alternatives considered

| Option | Why not |
|---|---|
| Display on/off from `pmset` | counts up to 2 h idle per break (display sleep 120) and anything that keeps the screen awake |
| macOS unified log (`log show`) for lock/unlock | slow, retention unclear; `pmset` already has unlocks |
| Screen Time / Biome data | undocumented binary format, needs Full Disk Access |
| A sampler that records idle time every minute | only works from install on; the power log already samples every 15 min |
| LaunchAgent for the history job | `~/Library/LaunchAgents` is root-owned on managed Macs; cron works everywhere |
| JXA (`osascript`) instead of Python | always present without Command Line Tools, but a full rewrite with no SQLite or curses |
| pip packages (e.g. rich, prompt_toolkit) | no clear benefit over the standard library; would break "native Python only" |
| Per-run flags for the tuning numbers | made `--help` long; numbers rarely change, so `--settings` stores them |

## 8. Testing and CI (R22)

- **Suite:** `python3 -m unittest discover -s tests`. It has 44 tests in `tests/test_timesheet.py`, using only the standard library.
- **Isolation:**
  - Each test uses a throwaway home folder, fake power logs, fake browser databases and a fake `crontab` command.
  - The real power log, browsers and crontab are never touched.
  - Tests set `TZ=UTC`.
- **Coverage:**
  - power-log parsing and active blocks
  - archive duplicates, time-zone and padding changes, retention
  - project labels, nearest-signal, overlap and gap-filling rules, place projects, splits over projects by % (start and 25% steps)
  - day start, rounding, estimates
  - all three browser history formats, time in front (Chromium, Firefox), editor saves, Claude prompts, Codex and Gemini sessions
  - cron install and removal, and commands that hang (timeouts)
  - places: probe parsing without serials, the log (changes only, one year, mode 600), setup labels and guesses, the `--collect` consent check
  - the command line end to end: setup, report, history and places questions, place labels and projects, `--settings`, `--help`, uninstall
  - the arrow-key folder browser and split slider, driven through a pseudo-terminal (arrows sent as `ESC O x`, the codes keypad mode expects, and `ESC [ x`)
- **CI** (`.github/workflows/ci.yml`):
  - **lint:** `ruff check` and `ruff format --check`, with ruff fixed at 0.17.0 and settings in `ruff.toml` (Python 3.9, line length 100, rules E, F, W, I, B)
  - **test:** the suite on a macOS runner's own `/usr/bin/python3` (the Command Line Tools 3.9 users run) and on Python 3.14 on Linux. `ubuntu-latest` is Ubuntu 26.04 since October 2026, which has no Python 3.9 for `setup-python`.
  - **badge:** shown in the README
  - **pinact:** every action pinned to a full SHA, version comments correct, no release younger than 10 days
  - **commits:** every commit since the last tag is a Conventional Commit (git-cliff)
- **CodeQL** (`.github/workflows/codeql.yml`): Python and workflow files, on push, pull request and weekly.
- **Scorecard** (`.github/workflows/scorecard.yml`): OpenSSF Scorecard, weekly and on each push to `main`, results published for the badge.

## 9. Known limits

- **15-min sampling.** A break shorter than about 15 min with no lock/unlock or sleep in it isn't seen. A return without an unlock or user wake is estimated to within about 7.5 min.
- **No input = not active.** Reading or thinking without touching the keyboard or mouse for longer than `break` counts as a break. Teams calls are the only exception.
- **Labels are by time, not by app.**
  - Other activity within 5 min of project work takes an even share; further away it only wins a slot when it is closer than the nearest project signal. Claude Code working on its own nearby can still win a slot for a project.
  - Projects busy within 5 min of each other share the slot evenly, whatever each one's real share of attention. An agent working alone in one folder while you work in another still takes half.
  - Apps that leave no signal (Slack, Outlook, Mail, …) count as other.
  - Browser time in front only fills gaps and is placed from the visit start, so a page read in several sittings is skipped.
- **Estimates come out a little low**: meetings, reading and work outside configured folders and profiles leave no signal. Calibration: 96.5% of the measured week.
- **Older weeks get fewer signals.**
  - File modification times keep only the latest change per file.
  - git reflog and Chrome history keep about 90 days; VS Code keeps 50 saves per file.
  - Claude transcripts keep 30 days; the prompt history keeps everything.
- **Places are noted hourly.** A move shows at the next run the Mac is awake for, so a place can be up to an hour late, and a shorter visit can be missed.
- **Places look the same when their parts do:** two units of a model without a serial, or two networks with the same router address and neither a domain nor a readable hardware address.
- **Unconfirmed meanings,** inferred and not documented by Apple, Google or Mozilla:
  - the loginwindow entry as an unlock
  - the synced-visit markers (Chromium `originator_cache_guid`, Firefox `source = 1`, Safari `origin`)

## 10. Validation status

| Item | Status |
|---|---|
| Power-log parsing, blocks, idle tail | checked by hand against one week of real log |
| Python 3.9 compatibility | tests pass on Apple's 3.9.6, locally and on the macOS runner in CI |
| Browser discovery on real data | checked: 8 Chrome profiles + Safari |
| Browser history reading, editor saves | checked with fake databases and files in the tests |
| Codex and Gemini CLI logs | read on real logs: 210 Codex events and 27 Gemini messages, all with a folder |
| Browser time in front | columns and units checked on real Chrome history. On a measured week it moved 5 min from other to work with all signals, 1.9 h of 70 h with browser visits alone |
| Setup, folder browser, history question, `--settings`, `--uninstall` | checked by the test suite (fake home and crontab) |
| Real `crontab` | not covered by the tests, which use a fake `crontab` |
| Place probes | run on macOS 26: Wi-Fi with a VPN up, one external screen, 6 USB devices; 0.11 s, 376 bytes per record. Ethernet and customer networks not seen yet |
| Timeouts | checked by the tests with hanging fake commands |
| CI | lint, Python 3.9 and 3.14, pinact on every push |
| pinact 10-day rule | checked: pinact 4.0.0 passes as committed and fails (`min-age violation`, exit 2) with a 3-day-old CodeQL release |
| CodeQL | no alerts |
| OpenSSF Scorecard | 6.4 / 10. The rest needs branch protection, reviewed pull requests or project age |

## 11. Open questions

- Other AI agents (OpenCode, Copilot CLI, Amp, Cursor CLI): not read yet, since none was on the test Mac to check its log format.
- Safari 17 profiles: read them too? The location of their history files still needs checking.
- Other meeting apps (Zoom, Google Meet, Webex) could be detected like Teams if they hold a display-sleep assertion during calls.
- Places: use the VPN in the labels? Count a customer place for that customer's project? Tune both once a few weeks are noted.
