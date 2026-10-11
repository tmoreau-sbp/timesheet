"""Tests for timesheet.py: python3 -m unittest discover -s tests -v

Everything runs against a throwaway home folder, fake logs and a fake crontab,
so the tests never touch the real power log, browsers or crontab.
"""

import fcntl
import json
import os
import pty
import select
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time as clock
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ["TZ"] = "UTC"
clock.tzset()
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "timesheet.py"
sys.path.insert(0, str(ROOT))
import timesheet as ts  # noqa: E402

TICKLE = (
    'PID 599(WindowServer) {action} UserIsActive "com.apple.iohideventsystem.queue.tickle '
    'serviceID:1 service:X product:{product} eventType:3" {age}  id:0x1 [System: DeclUser]'
)


def line(when, kind, msg):
    return f"{when:%Y-%m-%d %H:%M:%S} +0000 {kind:<20}\t{msg}"


def tickle(when, age="00:00:00", action="Summary", product="Apple Internal Keyboard"):
    return line(when, "Assertions", TICKLE.format(action=action, product=product, age=age))


def unlock(when):
    return line(
        when,
        "Assertions",
        'PID 603(loginwindow) Created UserIsActive "Loginwindow User Activity" 00:00:00  id:0x2',
    )


def at(hh, mm=0, day=None):
    day = day or datetime(2026, 10, 6)
    return day.replace(hour=hh, minute=mm)


def fake_crontab(folder):
    """A crontab command that keeps its table in folder/crontab.txt."""
    path = Path(folder) / "crontab"
    path.write_text(f"""#!/bin/sh
F={folder}/crontab.txt
if [ "$1" = "-l" ]; then [ -f "$F" ] && cat "$F" && exit 0; echo "no crontab" >&2; exit 1; fi
if [ "$1" = "-" ]; then cat > "$F"; exit 0; fi
""")
    path.chmod(0o755)
    return Path(folder) / "crontab.txt"


class PowerLog(unittest.TestCase):
    def test_input_time_is_line_time_minus_age(self):
        inputs, idle, _, _ = ts.parse_pmset(tickle(at(10, 15), "00:05:00"))
        self.assertEqual(inputs, [(at(10, 10), False)])
        self.assertEqual(idle, [(at(10, 10), at(10, 15))])

    def test_unlock_user_wake_and_created_mark_a_return(self):
        text = "\n".join(
            [
                unlock(at(9)),
                line(
                    at(9, 1), "Wake", "Wake from Deep Idle [CDNVA] : due to UserActivity Assertion"
                ),
                tickle(at(9, 2), action="Created"),
            ]
        )
        inputs, _, _, _ = ts.parse_pmset(text)
        self.assertEqual(inputs, [(at(9), True), (at(9, 1), True), (at(9, 2), True)])

    def test_caffeinate_and_sound_card_are_not_input(self):
        text = "\n".join(
            [
                line(
                    at(9),
                    "Assertions",
                    'PID 7(caffeinate) Created UserIsActive "caffeinate command-line tool" '
                    "00:00:00",
                ),
                tickle(at(9, 5), product="USB Sound Blaster HD"),
            ]
        )
        inputs, _, _, _ = ts.parse_pmset(text)
        self.assertEqual(inputs, [])

    def test_teams_call_and_sleep(self):
        text = "\n".join(
            [
                line(
                    at(9),
                    "Assertions",
                    "PID 9(MSTeams) Created NoDisplaySleepAssertion "
                    '"Microsoft Teams Call in progress" '
                    "00:00:00",
                ),
                line(
                    at(9, 30),
                    "Assertions",
                    "PID 9(MSTeams) Released NoDisplaySleepAssertion "
                    '"Microsoft Teams Call in progress" '
                    "00:30:00",
                ),
                line(at(12), "Sleep", "Entering Sleep state due to 'Idle Sleep'"),
                line(
                    at(13),
                    "Wake",
                    "DarkWake to FullWake from Deep Idle [CDNVA] : due to Notification",
                ),
            ]
        )
        _, idle, calls, _ = ts.parse_pmset(text)
        self.assertEqual(calls, [(at(9), at(9, 30))])
        self.assertIn((at(12), at(13)), idle)


class ActiveBlocks(unittest.TestCase):
    def blocks(self, *lines):
        inputs, idle, calls, _ = ts.parse_pmset("\n".join(lines))
        return ts.active_blocks(inputs, idle, calls, timedelta(minutes=10))

    def test_15_min_samples_of_continuous_work_join(self):
        samples = [tickle(at(9) + timedelta(minutes=15 * i), "00:00:20") for i in range(9)]
        self.assertEqual(len(self.blocks(unlock(at(9)), *samples)), 1)

    def test_idle_tail_is_cut_and_break_splits(self):
        blocks = self.blocks(
            unlock(at(9)),
            tickle(at(12, 47), "00:01:00"),  # last input 12:46
            tickle(at(13, 47), "01:01:00"),  # still 12:46: lunch
            unlock(at(13, 54)),
            tickle(at(14, 2)),
        )
        self.assertEqual(blocks, [[at(9), at(12, 46)], [at(13, 54), at(14, 2)]])

    def test_return_seen_only_by_a_sample_starts_at_the_midpoint(self):
        blocks = self.blocks(
            unlock(at(9)),
            tickle(at(9, 15), "00:15:00"),  # idle since 09:00
            tickle(at(9, 30), "00:30:00"),  # still idle at 09:30
            tickle(at(9, 45)),  # back somewhere in 09:30..09:45
        )
        self.assertEqual(
            blocks[-1][0],
            at(
                9,
                37,
            )
            + timedelta(seconds=30),
        )


class Archive(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "archive.log"
        now = datetime.now().replace(microsecond=0)
        self.log = "\n".join(tickle(now - timedelta(hours=h)) for h in range(3))

    def tearDown(self):
        self.dir.cleanup()

    def test_saving_twice_stores_each_event_once(self):
        ts.update_archive(self.path, self.log)
        first = self.path.read_text()
        ts.update_archive(self.path, self.log)
        self.assertEqual(self.path.read_text(), first)
        self.assertEqual(len(first.splitlines()), 3)

    def test_same_event_in_other_time_zone_or_padding_is_a_duplicate(self):
        ts.update_archive(self.path, self.log)
        shifted = []
        for ln in self.log.splitlines():
            when = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=2)
            shifted.append(f"{when:%Y-%m-%d %H:%M:%S} +0200 " + ln[26:].replace("\t", "   "))
        ts.update_archive(self.path, "\n".join(shifted))
        self.assertEqual(len(self.path.read_text().splitlines()), 3)

    def test_lines_older_than_a_year_are_dropped(self):
        old = tickle(datetime.now() - timedelta(days=ts.ARCHIVE_DAYS + 5))
        ts.update_archive(self.path, old + "\n" + self.log)
        self.assertEqual(len(self.path.read_text().splitlines()), 3)

    def test_not_written_without_consent_or_change(self):
        ts.update_archive(self.path, self.log, save=False)
        self.assertFalse(self.path.exists())
        ts.update_archive(self.path, self.log)
        mtime = self.path.stat().st_mtime_ns
        ts.update_archive(self.path, self.log)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)


class Labels(unittest.TestCase):
    def setUp(self):
        home = str(ts.HOME)
        self.projects = ts.Projects(
            {
                "projects": ["A", "B"],
                "folders": {"~/work": "A", "~/work/b": "B"},
                "browsers": {"Chrome me@work.com": "A"},
                "meetings": "B",
            }
        )
        self.home = home

    def test_most_specific_folder_wins(self):
        self.assertEqual(self.projects.of_path(f"{self.home}/work/x/y"), "A")
        self.assertEqual(self.projects.of_path(f"{self.home}/work/b/z"), "B")
        self.assertEqual(self.projects.of_path(f"{self.home}/workshop"), ts.OTHER)
        self.assertEqual(self.projects.roots(), [f"{self.home}/work"])

    def test_browser_profile_match_ignores_case(self):
        self.assertEqual(self.projects.of_browser("chrome ME@work.com"), "A")
        self.assertEqual(self.projects.of_browser("Safari"), ts.OTHER)

    def test_nearest_signal_wins_calls_go_to_meetings(self):
        label = ts.Labeler(
            [(at(10), "A"), (at(10, 20), "B")],
            [(at(12), at(12, 30))],
            timedelta(minutes=15),
            self.projects.meetings,
        )
        self.assertEqual(label(at(10, 5)), "A")
        self.assertEqual(label(at(10, 12)), "B")
        self.assertEqual(label(at(11)), ts.OTHER)
        self.assertEqual(label(at(12, 10)), "B")

    def test_page_in_front_only_fills_gaps(self):
        label = ts.Labeler(
            [(at(10), "A")],
            [],
            timedelta(minutes=15),
            self.projects.meetings,
            [(at(10), at(11), "B"), (at(10, 50), at(11, 30), "C")],
        )
        self.assertEqual(label(at(10, 5)), "A")  # a signal nearby wins
        self.assertEqual(label(at(10, 30)), "B")
        self.assertEqual(label(at(10, 55)), "C")  # latest started page
        self.assertEqual(label(at(11, 45)), ts.OTHER)


class Days(unittest.TestCase):
    def test_day_starts_at_03_and_rounds_to_5_min(self):
        day = at(3)
        rows = ts.day_rows(
            [[at(23, 2), datetime(2026, 10, 7, 0, 44)]],
            day,
            lambda t: "A",
            timedelta(minutes=5),
            timedelta(minutes=5),
        )
        self.assertEqual(
            [(r[0], r[1] - r[0], r[2]) for r in rows],
            [(at(23), timedelta(hours=1, minutes=45), "A")],
        )

    def test_short_blocks_are_dropped(self):
        rows = ts.day_rows(
            [[at(9), at(9, 3)]], at(3), lambda t: "A", timedelta(minutes=5), timedelta(minutes=5)
        )
        self.assertEqual(rows, [])

    def test_estimates_group_signals_before_the_archive(self):
        times = [at(9), at(9, 10), at(9, 20), at(11), at(12)]
        self.assertEqual(
            ts.estimate_blocks(times, at(11, 30), timedelta(minutes=15)),
            [[at(9), at(9, 20)], [at(11), at(11)]],
        )


class Sources(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def history_db(self, name, schema, rows):
        path = self.tmp / name
        con = sqlite3.connect(path)
        con.execute(schema)
        table = schema.split()[2]
        con.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
        con.commit()
        con.close()
        return path

    def test_browser_visits_skip_visits_synced_from_other_devices(self):
        since = datetime(2026, 1, 1)
        when = datetime(2026, 10, 6, 10)
        cases = {
            "chromium": (
                "CREATE TABLE visits (visit_time INTEGER, originator_cache_guid TEXT)",
                (when - datetime(1601, 1, 1)) // timedelta(microseconds=1),
                ("", "phone"),
            ),
            "firefox": (
                "CREATE TABLE moz_historyvisits (visit_date INTEGER, source INTEGER)",
                (when - datetime(1970, 1, 1)) // timedelta(microseconds=1),
                (0, 1),
            ),
            "safari": (
                "CREATE TABLE history_visits (visit_time REAL, origin INTEGER)",
                (when - datetime(2001, 1, 1)).total_seconds(),
                (0, 1),
            ),
        }
        for kind, (schema, value, (local, synced)) in cases.items():
            with self.subTest(kind):
                db = self.history_db(f"{kind}.db", schema, [(value, local), (value, synced)])
                self.assertEqual(ts.browser_visits(kind, db, since, quiet=True), ([when], []))

    def test_pages_in_front_for_most_of_their_visit_become_spans(self):
        since = datetime(2026, 1, 1)
        when = datetime(2026, 10, 6, 10)
        us = timedelta(microseconds=1)
        mins = timedelta(minutes=1) // us
        chrome = sqlite3.connect(self.tmp / "History")
        chrome.executescript(
            "CREATE TABLE visits (id INTEGER, visit_time INTEGER, visit_duration INTEGER,"
            " originator_cache_guid TEXT);"
            "CREATE TABLE context_annotations (visit_id INTEGER, total_foreground_duration);"
        )
        start = (when - datetime(1601, 1, 1)) // us
        visits = [  # (in front, visit length, synced from)
            (40 * mins, 41 * mins, ""),  # kept
            (10 * mins, 60 * mins, ""),  # tab mostly behind others
            (40 * mins, 41 * mins, "phone"),  # another device
            (-1000000, 0, ""),  # not recorded
        ]
        for i, (front, length, guid) in enumerate(visits):
            chrome.execute("INSERT INTO visits VALUES (?, ?, ?, ?)", (i, start, length, guid))
            chrome.execute("INSERT INTO context_annotations VALUES (?, ?)", (i, front))
        chrome.commit()
        chrome.close()
        firefox = sqlite3.connect(self.tmp / "places.sqlite")
        firefox.executescript(
            "CREATE TABLE moz_historyvisits (visit_date INTEGER, source INTEGER);"
            "CREATE TABLE moz_places_metadata (created_at INTEGER, updated_at INTEGER,"
            " total_view_time INTEGER);"
        )
        ms = (when - datetime(1970, 1, 1)) // timedelta(milliseconds=1)
        firefox.execute(
            "INSERT INTO moz_places_metadata VALUES (?, ?, ?)", (ms, ms + 2400000, 2400000)
        )
        firefox.execute(
            "INSERT INTO moz_places_metadata VALUES (?, ?, ?)", (ms, ms + 3600000, 600000)
        )
        firefox.commit()
        firefox.close()
        for kind, name in (("chromium", "History"), ("firefox", "places.sqlite")):
            with self.subTest(kind):
                _, spans = ts.browser_visits(kind, self.tmp / name, since, quiet=True)
                self.assertEqual(spans, [(when, when + timedelta(minutes=40))])

    def test_browser_profiles_are_named_by_account(self):
        chrome = self.tmp / "Google/Chrome"
        (chrome / "Profile 1").mkdir(parents=True)
        (chrome / "Profile 1/History").write_text("")
        (chrome / "Local State").write_text(
            json.dumps({"profile": {"info_cache": {"Profile 1": {"user_name": "me@work.com"}}}})
        )
        fox = self.tmp / "Firefox/Profiles/ab12.default-release"
        fox.mkdir(parents=True)
        (fox / "places.sqlite").write_text("")
        with (
            mock.patch.object(ts, "APP_SUPPORT", self.tmp),
            mock.patch.object(ts, "HOME", self.tmp),
        ):
            keys = [k for k, _, _ in ts.browser_profiles()]
        self.assertEqual(keys, ["Chrome me@work.com", "Firefox default-release"])

    def test_editor_saves_and_claude_prompts_give_paths(self):
        entry = self.tmp / "Code/User/History/x1"
        entry.mkdir(parents=True)
        stamp = datetime(2026, 10, 6, 10).timestamp() * 1000
        (entry / "entries.json").write_text(
            json.dumps(
                {
                    "resource": "file:///Users/me/my%20work/main.py",
                    "entries": [{"timestamp": stamp}],
                }
            )
        )
        history = self.tmp / "history.jsonl"
        history.write_text(
            json.dumps(
                {"timestamp": stamp, "project": "/Users/me/work", "display": "secret prompt"}
            )
            + "\nnot json\n"
        )
        since = datetime(2026, 1, 1)
        with (
            mock.patch.object(ts, "APP_SUPPORT", self.tmp),
            mock.patch.object(ts, "CLAUDE_HISTORY", history),
        ):
            self.assertEqual(
                ts.editor_saves(since), [(datetime(2026, 10, 6, 10), "/Users/me/my work/main.py")]
            )
            self.assertEqual(
                ts.claude_prompts(since), [(datetime(2026, 10, 6, 10), "/Users/me/work")]
            )

    def test_codex_and_gemini_sessions_give_folders(self):
        codex = self.tmp / ".codex"
        (codex / "sessions/2026/10/06").mkdir(parents=True)
        events = [
            {
                "timestamp": "2026-10-06T10:00:00.000Z",
                "type": "session_meta",
                "payload": {"cwd": "/Users/me/work"},
            },
            {
                "timestamp": "2026-10-06T10:01:00.000Z",
                "type": "event_msg",
                "payload": {"type": "exec_command_end", "cwd": "/tmp"},
            },
            {
                "timestamp": "2026-10-06T10:02:00.000Z",
                "type": "turn_context",
                "payload": {"cwd": "/Users/me/work/b"},
            },
            {"timestamp": "not a time", "type": "event_msg", "payload": {}},
        ]
        (codex / "sessions/2026/10/06/rollout-1.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\nnot json\n"
        )
        gemini = self.tmp / ".gemini"
        for name, root in (("work", "/Users/me/work"), ("old", None)):
            (gemini / "tmp" / name / "chats").mkdir(parents=True)
            if root:
                (gemini / "tmp" / name / ".project_root").write_text(root + "\n")
            (gemini / "tmp" / name / "chats/session-1.json").write_text(
                json.dumps({"messages": [{"timestamp": "2026-10-06T11:00:00.000Z"}]})
            )
        (gemini / "projects.json").write_text(json.dumps({"projects": {"/Users/me/old": "old"}}))
        since = datetime(2026, 1, 1)
        with (
            mock.patch.object(ts, "CODEX_HOME", codex),
            mock.patch.object(ts, "GEMINI_HOME", gemini),
        ):
            self.assertEqual(
                ts.codex_sessions(since),
                [
                    (at(10), "/Users/me/work"),
                    (at(10, 1), "/Users/me/work"),
                    (at(10, 2), "/Users/me/work/b"),
                ],
            )
            self.assertEqual(
                sorted(ts.gemini_sessions(since)),
                [(at(11), "/Users/me/old"), (at(11), "/Users/me/work")],
            )


class Cron(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.table = fake_crontab(self.dir.name)
        self.path = mock.patch.dict(os.environ, PATH=f"{self.dir.name}:{os.environ['PATH']}")
        self.path.start()

    def tearDown(self):
        self.path.stop()
        self.dir.cleanup()

    def test_install_replaces_a_hand_made_line_and_keeps_others(self):
        self.table.write_text("0 9 * * 1 echo mine\n0 * * * * python3 ~/timesheet.py --collect\n")
        archive = Path(self.dir.name) / "Application Support" / "archive.log"
        with mock.patch("sys.stdout"):
            ts.install_cron(archive)
        lines = self.table.read_text().splitlines()
        self.assertEqual(lines[0], "0 9 * * 1 echo mine")
        self.assertEqual(len(lines), 2)
        self.assertIn(ts.CRON_TAG, lines[1])
        self.assertIn("'", lines[1])  # path with a space is quoted
        with mock.patch("sys.stdout"):
            ts.uninstall_cron()
        self.assertEqual(self.table.read_text().splitlines(), ["0 9 * * 1 echo mine"])
        self.assertFalse(ts.cron_installed())


class CommandLine(unittest.TestCase):
    """The script run as users run it, in a throwaway home folder."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        self.table = fake_crontab(self.dir.name)
        self.table.write_text("0 9 * * 1 echo mine\n")
        for folder in ("work/a", "work/b", "personal"):
            (self.home / folder).mkdir(parents=True)
        yesterday = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=1
        )
        prompts = [
            {
                "timestamp": (yesterday.replace(hour=9) + timedelta(minutes=20 * i)).timestamp()
                * 1000,
                "project": str(self.home / "work/a"),
            }
            for i in range(7)
        ]
        (self.home / ".claude").mkdir()
        (self.home / ".claude/history.jsonl").write_text("\n".join(json.dumps(p) for p in prompts))
        log = [unlock(yesterday.replace(hour=9))]
        log += [tickle(yesterday.replace(hour=9) + timedelta(minutes=15 * i)) for i in range(1, 9)]
        self.log = self.home / "pmset.log"
        self.log.write_text("\n".join(log) + "\n")
        self.data = self.home / "Library/Application Support/timesheet"

    def tearDown(self):
        self.dir.cleanup()

    def run_tool(self, *args, answers=""):
        env = dict(
            os.environ,
            HOME=str(self.home),
            TZ="UTC",
            NO_COLOR="1",
            PATH=f"{self.dir.name}:{os.environ['PATH']}",
        )
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--pmset-file", str(self.log), *args],
            input=answers,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def configure(self, *answers):
        return self.run_tool("--configure", answers="\n".join(answers) + "\n")

    def first_setup(self):
        # projects; ~/work (only folder with activity) -> A; extra ~/work/b -> B; Teams -> A
        return self.configure("A, B", "1", "~/work/b", "2", "", "1")

    def second_setup(self, history):
        # same answers; ~/work/b is now listed; then the history question
        return self.configure("A, B", "1", "2", "", "1", history)

    def test_without_setup_it_warns_and_still_reports(self):
        result = self.run_tool("-n", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no projects set up yet", result.stderr)

    def test_first_setup_asks_no_history_question(self):
        result = self.first_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("history", result.stdout.lower())
        config = json.loads((self.data / "config.json").read_text())
        self.assertEqual(config["folders"], {"~/work": "A", "~/work/b": "B"})
        self.assertEqual(config["meetings"], "A")
        self.assertEqual(self.table.read_text(), "0 9 * * 1 echo mine\n")

    def test_report_without_history_saves_nothing(self):
        self.first_setup()
        result = self.run_tool("-n", "2", "--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("09:00   2h00", result.stdout)
        self.assertIn("work 2.00h", result.stdout)
        self.assertFalse((self.data / "pmset-archive.log").exists())

    def test_history_from_the_second_setup_then_full_uninstall(self):
        self.first_setup()
        result = self.second_setup("y")
        self.assertIn("Keep a history?", result.stdout)
        self.assertIn(ts.CRON_TAG, self.table.read_text())
        self.run_tool("-n", "1")
        self.assertTrue((self.data / "pmset-archive.log").exists())

        result = self.run_tool("--uninstall", answers="n\n")
        self.assertIn("Nothing removed", result.stdout)
        result = self.run_tool("--uninstall", answers="y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.table.read_text(), "0 9 * * 1 echo mine\n")

    def test_settings_are_saved_and_used(self):
        self.first_setup()
        # weeks 2; day start 99 is refused, then 5; the rest kept
        result = self.run_tool("--settings", answers="2\n99\n5\n\n\n\n\n")
        self.assertIn("answer a number from 0 to 23", result.stdout)
        config = json.loads((self.data / "config.json").read_text())
        self.assertEqual(config["settings"], {"weeks": 2, "day_start": 5})
        self.assertIn("day = 05:00-05:00", self.run_tool().stdout)
        # - resets weeks to its default, which is then not stored
        self.run_tool("--settings", answers="-\n\n\n\n\n\n")
        config = json.loads((self.data / "config.json").read_text())
        self.assertEqual(config["settings"], {"day_start": 5})

    def test_version_is_shown_and_stampable(self):
        self.assertIn("timesheet dev", self.run_tool("--help").stdout)
        # the release workflow replaces exactly this line
        lines = SCRIPT.read_text().splitlines()
        self.assertEqual(sum(ln.startswith('VERSION = "dev"') for ln in lines), 1)

    def test_help_lists_only_the_user_options(self):
        result = self.run_tool("--help")
        for option in (
            "--weeks",
            "--days",
            "--list",
            "--other",
            "--configure",
            "--settings",
            "--uninstall",
        ):
            self.assertIn(option, result.stdout)
        for hidden in ("--collect", "--pmset-file", "--archive"):
            self.assertNotIn(hidden, result.stdout)


class FolderBrowser(unittest.TestCase):
    """Arrow keys in a real (pseudo) terminal."""

    def test_assign_folder_and_subfolder_with_arrow_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for folder in ("work/a", "work/b"):
                (home / folder).mkdir(parents=True)
            (home / ".claude").mkdir()
            (home / ".claude/history.jsonl").write_text(
                json.dumps({"timestamp": 1.76e12, "project": str(home / "work/a")})
            )
            config = home / "Library/Application Support/timesheet/config.json"
            env = dict(os.environ, HOME=tmp, TERM="xterm-256color", TZ="UTC")
            pid, fd = pty.fork()
            if pid == 0:
                os.execve(
                    sys.executable,
                    [sys.executable, str(SCRIPT), "--configure"],
                    env,
                )
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            screen = b""

            def send(keys, wait=1.0):
                nonlocal screen
                os.write(fd, keys)
                end = clock.time() + wait
                while clock.time() < end:
                    if select.select([fd], [], [], 0.1)[0]:
                        try:
                            screen += os.read(fd, 65536)
                        except OSError:
                            return

            try:
                send(b"", 1.5)
                send(b"A, B\r", 2)  # projects, then the browser opens at ~
                send(b"1")  # ~/work (most activity) -> A
                send(b"\x1bOC")  # open ~/work: a (activity), then b
                send(b"\x1bOB")  # down to b
                send(b"2")  # ~/work/b -> B
                send(b"\x1bOD")  # back to ~
                send(b"\r", 2)  # done; no browsers in this home
                send(b"\r", 2)  # Teams: own meetings row
            finally:
                for _ in range(50):
                    done, _ = os.waitpid(pid, os.WNOHANG)
                    if done:
                        break
                    clock.sleep(0.1)
                else:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
            self.assertIn(b"(A)", screen)  # ~/work/a shows the inherited project
            saved = json.loads(config.read_text())
            self.assertEqual(saved["folders"], {"~/work": "A", "~/work/b": "B"})


if __name__ == "__main__":
    unittest.main()
