"""iCalendar 일정 어댑터의 파싱·비교·멱등 동기화를 검증한다.

실행:
    python -m unittest schedule.tests.test_sync_ical_schedule -v
"""
from __future__ import annotations

import sqlite3
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

from schedule.scripts.sync_ical_schedule import (
    KST,
    ScheduleEvent,
    compare_events,
    normalize_feed,
    open_db,
    read_legacy_csv,
    upsert_schedule,
    parse_summary,
    open_simulation_db,
    main,
)


ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:match-100@example.test
DTSTART:20261024T170000Z
SUMMARY:(Group Stage) Round 2: AAA vs BBB
DESCRIPTION:결과 데이터는 어댑터가 읽지 않는다
END:VEVENT
BEGIN:VEVENT
UID:match-101@example.test
DTSTART:20261025T180000Z
SUMMARY:(Group Stage) Round 3: TBD vs CCC
END:VEVENT
BEGIN:VEVENT
UID:match-old@example.test
DTSTART:20251024T170000Z
SUMMARY:(Group Stage) Round 2: AAA vs BBB
END:VEVENT
END:VCALENDAR
"""

LEGACY_CSV = """Subject,Start Date,Start Time
2026 Season World Championship Main Event - AAA vs BBB,2026-10-25,02:00
"""


def event(
    source_id: str = "match-100@example.test",
    team_a: str = "AAA",
    team_b: str = "BBB",
    start: str = "2026-10-25T02:00:00+09:00",
) -> ScheduleEvent:
    return ScheduleEvent(
        source_id,
        "Group Stage Round 2",
        team_a,
        team_b,
        datetime.fromisoformat(start).astimezone(KST),
    )


class IcalParsing(unittest.TestCase):
    def test_pandascore_aliases_match_vod_teams(self):
        self.assertEqual(parse_summary("(Group Stage) Round 1: TL vs LY")[1:], ("TLAW", "LYON"))
    def test_truncated_calendar_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_feed(ICS.replace("END:VCALENDAR", ""), 2026)

    def test_result_in_team_field_is_rejected(self):
        self.assertIsNone(parse_summary("(Group Stage) Round 2: AAA vs BBB 2-0"))

    def test_filters_other_seasons_and_unresolved_matchups(self):
        events, stats = normalize_feed(ICS, 2026)

        self.assertEqual(events, [event()])
        self.assertEqual(stats.scanned, 3)
        self.assertEqual(stats.unresolved, 1)
        self.assertEqual(stats.out_of_scope, 1)
        self.assertEqual(stats.malformed, 0)

    def test_folded_summary_is_unfolded(self):
        text = ICS.replace(
            "SUMMARY:(Group Stage) Round 2: AAA vs BBB",
            "SUMMARY:(Group Stage) Round 2: AAA\n  vs BBB",
        )
        events, _ = normalize_feed(text, 2026)
        self.assertEqual(events[0].team_a, "AAA")
        self.assertEqual(events[0].team_b, "BBB")


class LegacyComparison(unittest.TestCase):
    def test_existing_lck_rte_csv_matches_legacy_importer(self):
        from schedule.scripts import import_schedule as legacy
        root = Path(__file__).resolve().parents[2]
        for path in sorted((root / "data" / "schedules").glob("*.ics")):
            with self.subTest(path=path.name):
                connection = sqlite3.connect(":memory:")
                connection.executescript(legacy.SCHEMA)
                legacy.upsert_matches(connection, legacy.read_rows(path), path.name, "test")
                expected = {
                    (row[0], tuple(sorted(row[1:3])), row[3])
                    for row in connection.execute(
                        "SELECT match_date,team_a,team_b,start_at_kst FROM matches "
                        "WHERE team_a != 'TBD' AND team_b != 'TBD'"
                    )
                }
                actual, stats = read_legacy_csv(path, 2026)
                self.assertEqual(stats.malformed, 0)
                self.assertEqual(
                    {(e.match_date, tuple(sorted((e.team_a, e.team_b))), e.start_iso) for e in actual},
                    expected,
                )
                connection.close()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tempdir.name) / "worlds.ics"
        self.csv_path.write_text(LEGACY_CSV, encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_equivalent_legacy_csv_passes(self):
        feed_events, _ = normalize_feed(ICS, 2026)
        legacy_events, stats = read_legacy_csv(self.csv_path, 2026)
        result = compare_events(feed_events, legacy_events)

        self.assertEqual(stats.malformed, 0)
        self.assertTrue(result.compatible)
        self.assertEqual(result.matched, 1)
        self.assertEqual(result.additional_in_feed, set())

    def test_time_difference_fails_without_tolerance(self):
        changed = LEGACY_CSV.replace("02:00", "02:05")
        self.csv_path.write_text(changed, encoding="utf-8")
        feed_events, _ = normalize_feed(ICS, 2026)
        legacy_events, _ = read_legacy_csv(self.csv_path, 2026)

        self.assertFalse(compare_events(feed_events, legacy_events).compatible)
        self.assertTrue(
            compare_events(feed_events, legacy_events, tolerance_minutes=5).compatible
        )

    def test_missing_feed_match_fails(self):
        legacy = LEGACY_CSV + (
            "2026 Season World Championship Main Event - CCC vs DDD,"
            "2026-10-26,03:00\n"
        )
        self.csv_path.write_text(legacy, encoding="utf-8")
        feed_events, _ = normalize_feed(ICS, 2026)
        legacy_events, _ = read_legacy_csv(self.csv_path, 2026)
        result = compare_events(feed_events, legacy_events)

        self.assertFalse(result.compatible)
        self.assertEqual(len(result.missing_from_feed), 1)


class DatabaseSync(unittest.TestCase):
    def test_fetch_failure_is_recorded_without_changing_matches(self):
        with patch("sys.argv", ["sync", "--season", "2026", "--apply", "--db", str(self.db_path)]):
            with patch("schedule.scripts.sync_ical_schedule.fetch_feed", side_effect=RuntimeError("수집 실패")):
                self.assertEqual(main(), 1)
        self.assertEqual(self.connection.execute("SELECT status,error FROM schedule_sync_runs").fetchone(), ("failed", "수집 실패"))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM matches").fetchone()[0], 0)

    def test_empty_comparison_csv_cannot_authorize_apply(self):
        feed = Path(self.tempdir.name) / "feed.ical"
        baseline = Path(self.tempdir.name) / "empty.csv"
        feed.write_text(ICS)
        baseline.write_text("Subject,Start Date,Start Time\n")
        with patch("sys.argv", ["sync", "--season", "2026", "--apply", "--db", str(self.db_path), "--feed-file", str(feed), "--compare-csv", str(baseline)]):
            self.assertEqual(main(), 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM matches").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT status FROM schedule_sync_runs").fetchone()[0], "failed")

    def test_dry_run_does_not_modify_source(self):
        simulation = open_simulation_db(self.db_path)
        upsert_schedule(simulation, [event()], "test-source", "WORLDS", 2026)
        simulation.close()
        self.assertEqual(self.connection.execute("SELECT count(*) FROM matches").fetchone()[0], 0)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "videos.sqlite"
        self.connection = open_db(self.db_path)

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def test_first_sync_and_repeat_are_idempotent(self):
        first = upsert_schedule(
            self.connection,
            [event()],
            "test-source",
            "WORLDS",
            2026,
        )
        second = upsert_schedule(
            self.connection,
            [event()],
            "test-source",
            "WORLDS",
            2026,
        )

        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM matches").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM schedule_events"
            ).fetchone()[0],
            1,
        )

    def test_schedule_change_keeps_match_id(self):
        upsert_schedule(
            self.connection,
            [event()],
            "test-source",
            "WORLDS",
            2026,
        )
        before = self.connection.execute(
            "SELECT match_id FROM matches"
        ).fetchone()[0]
        changed = event(team_b="CCC", start="2026-10-25T03:00:00+09:00")
        result = upsert_schedule(
            self.connection,
            [changed],
            "test-source",
            "WORLDS",
            2026,
        )
        after = self.connection.execute(
            "SELECT match_id, team_b, start_at_kst FROM matches"
        ).fetchone()

        self.assertEqual(result.updated, 1)
        self.assertEqual(after[0], before)
        self.assertEqual(after[1], "CCC")
        self.assertEqual(after[2], "2026-10-25T03:00:00+09:00")

    def test_existing_legacy_row_is_adopted(self):
        self.connection.execute(
            """
            INSERT INTO matches
              (match_id, league, season, stage, team_a, team_b,
               start_at_kst, match_date, source_file, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-id",
                "WORLDS",
                2026,
                "Main Event",
                "BBB",
                "AAA",
                "2026-10-25T02:00:00+09:00",
                "2026-10-25",
                "legacy.csv",
                "2026-10-01T00:00:00Z",
            ),
        )
        self.connection.commit()

        result = upsert_schedule(
            self.connection,
            [event()],
            "test-source",
            "WORLDS",
            2026,
        )
        rows = self.connection.execute(
            "SELECT match_id FROM matches"
        ).fetchall()
        mapping = self.connection.execute(
            "SELECT match_id FROM schedule_events"
        ).fetchone()[0]

        self.assertEqual(result.adopted, 1)
        self.assertEqual(rows, [("legacy-id",)])
        self.assertEqual(mapping, "legacy-id")

    def test_ambiguous_legacy_rows_are_not_guessed(self):
        row = (
            "WORLDS",
            2026,
            "Main Event",
            "AAA",
            "BBB",
            "2026-10-25T02:00:00+09:00",
            "2026-10-25",
            "legacy.csv",
            "2026-10-01T00:00:00Z",
        )
        for match_id in ("legacy-1", "legacy-2"):
            self.connection.execute(
                """
                INSERT INTO matches
                  (match_id, league, season, stage, team_a, team_b,
                   start_at_kst, match_date, source_file, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (match_id, *row),
            )
        self.connection.commit()

        result = upsert_schedule(
            self.connection,
            [event(source_id="new", team_a="DDD"), event()],
            "test-source",
            "WORLDS",
            2026,
        )

        self.assertEqual(result.conflicts, 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM matches").fetchone()[0], 2)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM schedule_events"
            ).fetchone()[0],
            0,
        )


class PipelineIntegration(unittest.TestCase):
    def test_utc_cron_covers_worlds_and_lck_hours(self):
        from datetime import timedelta
        cron = (Path(__file__).resolve().parents[2] / "deploy" / "crontab").read_text()
        lines = [line for line in cron.splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("MODE=fetch" in line for line in lines))
        fast_hours = set()
        for part in lines[0].split()[1].split(","):
            start, end = map(int, part.split("-"))
            fast_hours.update(range(start, end + 1))
        self.assertEqual({(hour + 9) % 24 for hour in fast_hours}, set(range(1, 13)) | set(range(17, 24)))
        self.assertIn(19, fast_hours)

    def test_schedule_failure_is_followed_by_vod_pipeline(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            python = Path(temp) / "python"
            python.write_text(
                '#!/bin/sh\n'
                'echo "실행: $1"\n'
                'case "$1" in *sync_ical_schedule.py) exit 1 ;; esac\n'
            )
            python.chmod(0o755)
            result = subprocess.run(
                ["bash", str(root / "deploy" / "entrypoint.sh")],
                cwd=root,
                env={**os.environ, "PATH": temp + os.pathsep + os.environ["PATH"],
                     "MODE": "fetch", "LCK_DB_PATH": str(Path(temp) / "test.sqlite")},
                capture_output=True, text=True, check=True,
            )
        self.assertIn("schedule_sync_runs 확인", result.stdout)
        self.assertEqual(
            [line for line in result.stdout.splitlines() if line.startswith("실행:")],
            ["실행: schedule/scripts/sync_ical_schedule.py",
             "실행: vod_index/scripts/fetch_chzzk_vods.py",
             "실행: vod_index/scripts/fetch_chzzk_vods.py",
             "실행: vod_index/scripts/classify_videos.py",
             "실행: vod_index/scripts/extract_winners.py"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
