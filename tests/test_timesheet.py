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


# Probe output as macOS prints it: a VPN up beside Wi-Fi, an LG screen beside the
# built-in one (no name), a trackball and a sound card with a serial number.
NWI = """Network information

IPv4 network interface information
   utun8 : flags      : 0x7 (IPv4,IPv6,DNS)
           address    : 10.200.1.2
     en0 : flags      : 0x5 (IPv4,DNS)
           address    : 192.168.1.20

   REACH : flags 0x00000002 (Reachable)

IPv6 network interface information
   utun8 : flags      : 0x7 (IPv4,IPv6,DNS)

Network interfaces: en0 utun8
"""
IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet6 fe80::1%utun0 prefixlen 64 scopeid 0x10
utun8: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 10.200.1.2 --> 10.200.1.2 netmask 0xffff0000
"""
NC_LIST = (
    "Available network connection services in the current set (*=enabled):\n"
    '* (Connected)      AAAA VPN (com.wireguard.macos) "Lab"     [VPN:com.wireguard.macos]\n'
    '* (Disconnected)   BBBB VPN (com.wireguard.macos) "Other"   [VPN:com.wireguard.macos]\n'
)
DISPLAYS = (
    '  |   "DisplayAttributes" = {"ProductAttributes"={"ManufacturerID"="00-10-fa"}}\n'
    '  |   "DisplayAttributes" = {"PortID"=0,"ProductAttributes"={"ManufacturerID"="GSM",'
    '"SerialNumber"=123456,"ProductName"="LG ULTRAFINE","AlphanumericSerialNumber"="SCREEN1"}}\n'
)
USB = """+-o Root  <class IORegistryEntry, id 0x100000100, retain 31>
  +-o AppleT8132USBXHCI@00000000  <class AppleT8132USBXHCI, id 0x1000002d1>
  | +-o USB Trackball@00110000  <class IOUSBHostDevice, id 0x100000c5b>
  |   {
  |     "idProduct" = 50184
  |     "USB Product Name" = "USB Trackball"
  |     "USB Vendor Name" = "Logitech"
  |     "idVendor" = 1133
  |   }
  | +-o USB Sound Blaster HD@00121000  <class IOUSBHostDevice, id 0x100000c60>
  |     "idProduct" = 12503
  |     "USB Product Name" = "USB Sound Blaster HD"
  |     "USB Serial Number" = "SOUND1"
  |     "USB Vendor Name" = "Creative Technology"
  |     "idVendor" = 1054
"""
PROBES = {
    (ts.SCUTIL, "--nwi"): NWI,
    (ts.IPCONFIG, "getsummary", "en0"): "  InterfaceType : WiFi\n",
    (ts.IPCONFIG, "getpacket", "en0"): (
        "router (ip_mult): {192.168.1.1}\ndomain_name (string): home.example\n"
    ),
    (
        ts.ARP,
        "-n",
        "192.168.1.1",
    ): "? (192.168.1.1) at a0:b1:c2:d3:e4:f5 on en0 ifscope [ethernet]\n",
    (ts.IFCONFIG,): IFCONFIG,
    (ts.SCUTIL, "--nc", "list"): NC_LIST,
    (ts.IOREG, "-l", "-w0", "-r", "-k", "DisplayAttributes"): DISPLAYS,
    (ts.IOREG, "-p", "IOUSB", "-w0", "-l"): USB,
}


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

    def test_projects_busy_at_once_share_the_slot_evenly(self):
        both = (("A", 50), ("B", 50))
        points = [(at(10), "A"), (at(10, 2), "B"), (at(10, 30), "A"), (at(10, 31), both)]
        points += [(at(11), "A"), (at(11, 1), ts.OTHER), (at(11, 40), "A"), (at(11, 41), ts.OTHER)]
        label = ts.Labeler(
            points,
            [(at(12), at(13))],
            timedelta(minutes=15),
            "meetings",
            place=lambda t: "B" if t > at(11, 30) else None,
            overlap=timedelta(minutes=5),
        )
        self.assertEqual(label(at(10, 1)), both)
        self.assertEqual(label(at(10, 9)), "B")  # A 9 min away: the nearest signal wins
        self.assertEqual(label(at(10, 31)), (("A", 75), ("B", 25)))  # a split shares too
        self.assertEqual(label(at(11)), (("A", 50), ("other", 50)))
        self.assertEqual(label(at(11, 40)), both)  # other's half goes to the place's project
        self.assertEqual(label(at(12, 30)), "meetings")  # calls still win
        off = ts.Labeler([(at(10), "A"), (at(10, 3), "B")], [], timedelta(minutes=15), "m")
        self.assertEqual(off(at(10, 1)), "A")  # without overlap: the nearest signal

    def test_place_project_only_fills_time_without_a_project(self):
        projects = ts.Projects(
            {"projects": ["A", "B"], "place_projects": {"customer": "B", "home": "Gone"}}
        )
        self.assertEqual(projects.places, {"customer": "B"})  # removed projects drop out
        where = {at(10): "customer", at(11): "customer", at(12): "home"}
        label = ts.Labeler(
            [(at(10), "A")],
            [(at(14), at(15))],
            timedelta(minutes=15),
            "meetings",
            place=lambda t: projects.places.get(where.get(t.replace(minute=0))),
        )
        self.assertEqual(label(at(10, 5)), "A")  # a project signal wins
        self.assertEqual(label(at(11, 30)), "B")  # no signal: the place's project
        self.assertEqual(label(at(12, 30)), ts.OTHER)  # a place without a project
        self.assertEqual(label(at(14, 30)), "meetings")

    def test_profile_and_teams_split_over_projects_by_share(self):
        projects = ts.Projects(
            {
                "projects": ["A", "B"],
                "browsers": {"Chrome x": {"A": 70, "B": 30}, "Chrome y": {"A": 50, "Gone": 50}},
                "meetings": {"A": 25, "B": 75},
            }
        )
        self.assertEqual(projects.of_browser("chrome x"), (("A", 70), ("B", 30)))
        self.assertEqual(projects.of_browser("Chrome y"), "A")  # a removed project drops out
        label = ts.Labeler(
            [(at(9), projects.of_browser("Chrome x"))],
            [(at(10), at(11))],
            timedelta(minutes=15),
            projects.meetings,
        )
        rows = ts.day_rows(
            [(at(9), at(9, 10)), (at(10), at(11))], at(3), label, timedelta(minutes=5), timedelta()
        )
        minutes = {k: round(v[0] / timedelta(minutes=1)) for k, v in ts.totals_of(rows).items()}
        self.assertEqual(minutes, {"A": 7 + 15, "B": 3 + 45})
        self.assertEqual(ts.label_text(projects.meetings), "A 25% + B 75%")

    def test_split_starts_even_and_moves_in_steps(self):
        self.assertEqual(ts.start_split(["A", "B"], {}), {"A": 50, "B": 50})
        self.assertEqual(ts.start_split(["A", "B", "C"], {}), {"A": 50, "B": 25, "C": 25})
        self.assertEqual(ts.start_split(["B", "A"], {"A": 60, "B": 40}), {"B": 40, "A": 60})
        split, names = {"A": 50, "B": 25, "C": 25}, ["A", "B", "C"]
        self.assertFalse(ts.shift(split, names, 0, more=True))  # B and C keep their 25%
        self.assertTrue(ts.shift(split, names, 1, more=True))  # C can't spare it, A can
        self.assertEqual(split, {"A": 25, "B": 50, "C": 25})
        self.assertTrue(ts.shift(split, names, 1, more=False))  # to the next one, C
        self.assertEqual(split, {"A": 25, "B": 25, "C": 50})
        self.assertFalse(ts.shift(split, names, 0, more=False))  # A keeps more than 0%

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


class Places(unittest.TestCase):
    def test_probe_reads_network_vpn_screens_and_usb_without_serials(self):
        record = ts.place_probe(lambda args: PROBES.get(tuple(args), ""))
        self.assertEqual(
            record,
            {
                "network": {
                    "type": "Wi-Fi",
                    "domain": "home.example",
                    "router": "192.168.1.1",
                    "id": ts.short_hash("a0:b1:c2:d3:e4:f5"),
                },
                "vpn": ["10.200.x.x", "Lab"],
                "screens": [f"LG ULTRAFINE #{ts.short_hash('SCREEN1')}"],
                "usb": [
                    "Creative Technology USB Sound Blaster HD 041e:30d7 #"
                    + ts.short_hash("SOUND1"),
                    "Logitech USB Trackball 046d:c408",
                ],
            },
        )
        text = json.dumps(record)
        for secret in ("a0:b1", "SCREEN1", "123456", "SOUND1", "10.200.1.2"):
            self.assertNotIn(secret, text)
        empty = {"network": None, "vpn": [], "screens": [], "usb": []}
        self.assertEqual(ts.place_probe(lambda args: ""), empty)

    def test_places_log_keeps_changes_only_for_a_year_user_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "places.log"
            now = datetime.now().astimezone()
            old = now - timedelta(days=ts.ARCHIVE_DAYS + 5)
            path.write_text(f"{old.isoformat()}\t{json.dumps({'usb': ['old']})}\n")
            home, office = {"usb": ["a"]}, {"usb": ["b"]}
            ts.save_place(path, home, now - timedelta(hours=2))
            ts.save_place(path, home, now - timedelta(hours=1))
            ts.save_place(path, office, now)
            self.assertEqual([r for _, r in ts.read_places(path)], [home, office])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_setups_name_places_and_new_ones_are_guessed(self):
        office = {"type": "Wi-Fi", "domain": "corp.example", "router": "10.0.0.1"}
        hotspot = {"type": "Wi-Fi", "router": "172.20.10.1"}
        ball = "Logitech USB Trackball 046d:c408"
        labels = {  # several setups can be the same place
            "office": [
                {"network": "Wi-Fi corp.example 10.0.0.1", "screens": ["Dell A"]},
                {"network": "Ethernet corp.example 10.0.0.1", "screens": []},
            ],
            "home": [{"network": "Wi-Fi 172.20.10.1", "screens": ["LG"]}],
            "train": [{"network": "Wi-Fi 172.20.10.1", "screens": []}],
        }
        entries = [
            (at(8), {"network": office, "screens": ["Dell A"], "usb": ["YubiKey 1050:0407"]}),
            (at(10), {"network": office, "screens": ["Dell B"]}),  # another desk: network
            (at(12), {"network": hotspot, "screens": ["LG"], "usb": [ball]}),
            (at(13), {"network": hotspot, "screens": []}),
            (at(14), {"network": hotspot, "screens": ["X"], "usb": [ball]}),  # hotspot: 2 places
            (at(15), {"network": None, "screens": ["LG"]}),  # no network: the screen
            (at(16), {"network": None, "screens": [], "usb": []}),
        ]
        place = ts.Places(entries, labels)
        self.assertEqual(
            [place(at(h, 30)) for h in (7, 8, 10, 12, 13, 14, 15, 16)],
            [None, "office", "office", "home", "train", "home", "home", None],
        )

    def test_collect_notes_the_place_only_when_asked(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            log = d / "pmset.log"
            log.write_text(tickle(datetime.now().replace(microsecond=0)) + "\n")
            config = d / "config.json"
            argv = ["timesheet.py", "--collect", "--pmset-file", str(log)]
            argv += ["--archive", str(d / "archive.log")]
            record = {"network": None, "vpn": [], "screens": [], "usb": ["x"]}
            with (
                mock.patch.object(ts, "DEFAULT_CONFIG", config),
                mock.patch.object(ts, "place_probe", return_value=record) as probe,
                mock.patch.object(sys, "argv", argv),
            ):
                ts.main()
                probe.assert_not_called()
                config.write_text(json.dumps({"keep_places": True}))
                ts.main()
            self.assertEqual([r for _, r in ts.read_places(d / "places.log")], [record])


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

    def test_a_hanging_command_is_given_up(self):
        (Path(self.dir.name) / "crontab").write_text("#!/bin/sh\nexec sleep 5\n")
        with (
            mock.patch.object(ts, "SLOW_TIMEOUT", 0.2),
            mock.patch.object(ts, "QUICK_TIMEOUT", 0.2),
        ):
            with self.assertRaises(SystemExit):
                ts.cron_lines()  # not [], which would drop the user's lines when written back
            with self.assertRaises(SystemExit):
                ts.write_crontab(["0 9 * * 1 echo mine"])
            self.assertEqual(ts.command_output(["sleep", "5"]), "")


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
        self.yesterday = yesterday
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

    def test_teams_calls_split_over_two_projects(self):
        call = self.yesterday.replace(hour=13)
        teams = 'PID 9(MSTeams) {} NoDisplaySleepAssertion "Microsoft Teams Call in progress" {}'
        with self.log.open("a") as f:
            # asleep from the morning's work until just before the call
            f.write(line(call.replace(hour=11, minute=5), "Sleep", "Entering Sleep state") + "\n")
            f.write(line(call.replace(hour=12, minute=55), "Wake", "Wake from Deep Idle") + "\n")
            f.write(line(call, "Assertions", teams.format("Created", "00:00:00")) + "\n")
            f.write(
                line(call + timedelta(hours=1), "Assertions", teams.format("Released", "01:00:00"))
            )
        # Teams: 1,9 and 1,1 are refused, then 1,2; 100% is refused, then A 60%
        result = self.configure("A, B", "1", "~/work/b", "2", "", "1,9", "1,1", "1,2", "100", "60")
        self.assertIn("answer 1-2 or project names", result.stdout)
        self.assertIn("answer a number from 1 to 99", result.stdout)
        config = json.loads((self.data / "config.json").read_text())
        self.assertEqual(config["meetings"], {"A": 60, "B": 40})
        self.assertIn("A: ~/work, Teams calls 60%", result.stdout)
        self.assertIn("B: ~/work/b, Teams calls 40%", result.stdout)
        report = self.run_tool("-n", "2", "--list").stdout
        self.assertIn("13:00   1h00  A 60% + B 40%", report)
        self.assertIn("work 3.00h: A 2.60h, B 0.40h", report)

    def test_places_opt_in_labels_report_and_removal(self):
        self.first_setup()
        # same answers as the second setup; history yes, places yes
        result = self.configure("A, B", "1", "2", "", "1", "y", "y")
        self.assertIn("Note where you work?", result.stdout)
        self.assertTrue(json.loads((self.data / "config.json").read_text())["keep_places"])
        places = self.data / "places.log"
        record = {
            "network": {"type": "Wi-Fi", "domain": "home.example", "router": "192.168.1.1"},
            "vpn": [],
            "screens": [],
            "usb": ["Logitech USB Trackball 046d:c408"],
        }
        places.write_text(
            f"{self.yesterday.replace(hour=8).isoformat()}+00:00\t{json.dumps(record)}\n"
        )
        # an hour of work at home after lunch, with no project signal
        afternoon = self.yesterday.replace(hour=14)
        with self.log.open("a") as f:
            f.write(
                line(afternoon.replace(hour=11, minute=5), "Sleep", "Entering Sleep state") + "\n"
            )
            f.write(
                line(afternoon.replace(hour=13, minute=55), "Wake", "Wake from Deep Idle") + "\n"
            )
            f.write(unlock(afternoon) + "\n")
            f.write("\n".join(tickle(afternoon + timedelta(minutes=15 * i)) for i in range(5)))
        self.assertNotIn("14:00", self.run_tool("-n", "2", "--list").stdout)
        # a label for the one setup (network, no screen), home -> B; history and places kept
        result = self.configure("A, B", "1", "2", "", "1", "home", "2", "y", "")
        self.assertIn("Places:", result.stdout)
        self.assertIn("B: ~/work/b, @home", result.stdout)
        config = json.loads((self.data / "config.json").read_text())
        self.assertEqual(
            config["places"],
            {"home": [{"network": "Wi-Fi home.example 192.168.1.1", "screens": []}]},
        )
        self.assertEqual(config["place_projects"], {"home": "B"})
        report = self.run_tool("-n", "2", "--list").stdout
        self.assertIn("09:00   2h00  A  @home", report)  # the folder's project wins
        self.assertRegex(report, r"(?m)^  14:00 .*  B  @home$")
        self.assertRegex(self.run_tool("-n", "2").stdout, r"(?m)^place\s+home\s")
        # no to places removes what was noted
        self.configure("A, B", "1", "2", "", "1", "", "", "y", "n")
        self.assertFalse(places.exists())

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


class ArrowKeys(unittest.TestCase):
    """Arrow keys in a real (pseudo) terminal."""

    def configure(self, home, steps):
        """Run --configure in a terminal, sending each (keys, wait); returns screen and config."""
        env = dict(os.environ, HOME=str(home), TERM="xterm-256color", TZ="UTC")
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, str(SCRIPT), "--configure"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        screen = b""
        try:
            for keys, wait in [(b"", 1.5)] + steps:
                os.write(fd, keys)
                end = clock.time() + wait
                while clock.time() < end:
                    if select.select([fd], [], [], 0.1)[0]:
                        try:
                            screen += os.read(fd, 65536)
                        except OSError:
                            break
        finally:
            for _ in range(50):
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                clock.sleep(0.1)
            else:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
        config = home / "Library/Application Support/timesheet/config.json"
        return screen, json.loads(config.read_text())

    def test_assign_folder_and_subfolder_with_arrow_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for folder in ("work/a", "work/b"):
                (home / folder).mkdir(parents=True)
            (home / ".claude").mkdir()
            (home / ".claude/history.jsonl").write_text(
                json.dumps({"timestamp": 1.76e12, "project": str(home / "work/a")})
            )
            screen, saved = self.configure(
                home,
                [
                    (b"A, B\r", 2),  # projects, then the browser opens at ~
                    (b"1", 1),  # ~/work (most activity) -> A
                    (b"\x1bOC", 1),  # open ~/work: a (activity), then b
                    (b"\x1bOB", 1),  # down to b
                    (b"2", 1),  # ~/work/b -> B
                    (b"\x1bOD", 1),  # back to ~
                    (b"\r", 2),  # done; no browsers in this home
                    (b"\r", 2),  # Teams: own meetings row
                ],
            )
            self.assertIn(b"(A)", screen)  # ~/work/a shows the inherited project
            self.assertEqual(saved["folders"], {"~/work": "A", "~/work/b": "B"})

    def test_teams_split_with_arrow_keys_from_50_50(self):
        with tempfile.TemporaryDirectory() as tmp:
            screen, saved = self.configure(
                Path(tmp),
                [
                    (b"A, B\r", 2),
                    (b"\r", 2),  # no folders; no browsers in this home
                    (b"1,2\r", 1),  # Teams: A and B, the slider starts at 50/50
                    (b"\x1b[C\x1bOC", 1),  # A +25 = 75/25, then refused: B keeps 25%
                    (b"\x1b[B\x1b[C", 1),  # down to B, +25 = 50/50
                    (b"\x1b[A\x1b[D\r", 2),  # up to A, -25 = 25/75
                ],
            )
            self.assertIn(b" 50%", screen)
            self.assertIn(b"\a", screen)  # the refused step beeps
            self.assertEqual(saved["meetings"], {"A": 25, "B": 75})


if __name__ == "__main__":
    unittest.main()
