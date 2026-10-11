# timesheet

[![CI](https://github.com/tmoreau-sbp/timesheet/actions/workflows/ci.yml/badge.svg)](https://github.com/tmoreau-sbp/timesheet/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/tmoreau-sbp/timesheet/badge)](https://scorecard.dev/viewer/?uri=github.com/tmoreau-sbp/timesheet)
[![License: GPL-3.0](https://img.shields.io/github/license/tmoreau-sbp/timesheet)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey?logo=apple)](#quick-start)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen)](timesheet.py)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Fill in your timesheet from your Mac's own activity: start time and hours per day, split by project. For macOS, one Python file, nothing to install.

```text
Week 41  2026-10-05 .. 2026-10-11
           Mon 05   Tue 06   Wed 07   Thu 08   Fri 09   Sat 10   Sun 11     week
start       08:30    08:00    08:15    08:45    09:00        -        -
ClientA      6.25     5.50     7.00     4.75     6.00        -        -    29.50
Internal     1.50     2.25     0.75     3.00     1.25        -        -     8.75
all work     7.75     7.75     7.75     7.75     7.25        -        -    38.25
```

## Quick start

Needs macOS with `/usr/bin/python3` (Command Line Tools: `xcode-select --install`). No pip packages.

```sh
curl -fLO https://github.com/tmoreau-sbp/timesheet/releases/latest/download/timesheet.py
chmod +x timesheet.py
gh attestation verify timesheet.py -R tmoreau-sbp/timesheet   # optional: proves it was built from this repo
./timesheet.py
```

Or clone the repo: `git clone https://github.com/tmoreau-sbp/timesheet.git`.

The first run asks for your projects and what belongs to each:
- folders: arrow keys, `→` opens a folder, `1`–`9` assigns a project
- browser profiles and Teams calls: one project, or several (`1,2`), split with ←→ in 25% steps from 50/50

The second run asks once whether to keep a history beyond the 7 days macOS keeps, and whether to note where you work.

## Usage

| Command | Shows |
|---|---|
| `./timesheet.py` | last 6 weeks, work only |
| `./timesheet.py -w 2` | last 2 weeks |
| `./timesheet.py -n 7 --list` | last 7 days, one row per work period |
| `./timesheet.py --other` | also time that is not work |
| `./timesheet.py --configure` | projects, what belongs to them, places, history |
| `./timesheet.py --settings` | day start, rounding and other numbers |
| `./timesheet.py --uninstall` | remove the cron job and all saved data |

- Hours are decimal.
- A day runs from 03:00 to 03:00, so late-night work counts for the evening before.
- `*` (yellow) marks an estimate.

## How it works

- **Active time:** from the macOS power log (`pmset -g log`), which records how long ago you last used the keyboard or mouse. The wait before sleep and `caffeinate` don't count; Teams calls count even without typing.
- **Project:** each 5-min slot goes to the project of the nearest activity:
  - AI agents: Claude Code, Codex, Gemini CLI
  - editor saves (VS Code, Cursor)
  - file and git changes in project folders
  - browser visits (Chrome, Edge, Brave, Arc, Opera, Firefox, Safari)

  Where none is near, a page left open in front (Chrome-family, Firefox) fills the gap, e.g. a long read.
- **History:** macOS keeps ~7 days of power log. With history on, an hourly cron job saves it (1 year, at most ~12 MB). Days before that are estimated from activity alone and marked `*`.
- **Where (optional):** the hourly job can also note the network, VPN, screens and USB devices, one line each time they change (under 1 MB a year). Label each setup (network + screens) in `--configure`: home, office, a customer. Several setups can be the same place, and a new one is guessed from its network, screens or USB devices. Link a place to a project if you like: work there with no folder, profile or call counts for it. The calendar gets a `place` row and `--list` rows end with `@label`.

Design, findings and trade-offs: [docs/SPEC.md](docs/SPEC.md). Changes per version: [CHANGELOG.md](CHANGELOG.md), written automatically at each release.

## Privacy

- Nothing leaves your Mac.
- Only times and folders are read: never URLs, page titles, prompt text or file contents.
- Places (opt-in) keep names of networks, screens and USB devices; serial numbers and hardware addresses only as a short hash. Never the Wi-Fi name or a location.
- Saved data lives in `~/Library/Application Support/timesheet/` and `--uninstall` removes it.
- Safari history needs Full Disk Access for your terminal app.
- Found a security issue? See [SECURITY.md](SECURITY.md).

## Uninstall

```sh
./timesheet.py --uninstall   # cron job + saved data, asks first
rm timesheet.py              # the tool itself (or the cloned folder)
```

## Development

```sh
python3 -m unittest discover -s tests   # 43 tests, fake home folder, real data untouched
ruff check . && ruff format --check .   # lint and format (ruff 0.17)
```

CI runs both on every push and pull request, with Python 3.9 and 3.14. CodeQL and OpenSSF Scorecard scan the repo. pinact checks that every GitHub Action is pinned to a commit SHA and is at least 10 days old. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[GPL-3.0](LICENSE)
