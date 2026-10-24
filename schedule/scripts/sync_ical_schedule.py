#!/usr/bin/env python3
"""구독형 iCalendar 피드를 검증하고 matches 테이블에 동기화한다.

기본 소스는 PandaScore 기반 zlypher Worlds 피드다. 결과·스코어·종료 시각은
저장하지 않고 UID, 시작 시각, 단계, 매치업만 사용한다.

사용법:
    # DB를 변경하지 않고 2026 Worlds 동기화 결과 확인
    python schedule/scripts/sync_ical_schedule.py --season 2026

    # Leaguepedia MatchCalendarExport CSV와 미래 일정 비교
    python schedule/scripts/sync_ical_schedule.py --season 2026 \
        --compare-csv data/schedules/worlds-2026.ics \
        --from-date 2026-10-24

    # 검증 후 실제 DB에 반영
    python schedule/scripts/sync_ical_schedule.py --season 2026 --apply

    # 테스트용 로컬 피드
    python schedule/scripts/sync_ical_schedule.py --season 2026 \
        --feed-file /tmp/worlds.ical --db /tmp/videos.sqlite --apply
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import os
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = (
    Path(os.environ["LCK_DB_PATH"])
    if os.environ.get("LCK_DB_PATH")
    else ROOT / "data" / "vods" / "videos.sqlite"
)
DEFAULT_URL = (
    "https://www.tompka.io/lol-events/cal/"
    "league-of-legends-world-championship.ical"
)
DEFAULT_SOURCE = "zlypher-worlds"
KST = ZoneInfo("Asia/Seoul")
MAX_FEED_BYTES = 2 * 1024 * 1024

logger = logging.getLogger(__name__)

MATCHES_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id      TEXT PRIMARY KEY,
    league        TEXT NOT NULL,
    season        INTEGER NOT NULL,
    stage         TEXT NOT NULL,
    team_a        TEXT NOT NULL,
    team_b        TEXT NOT NULL,
    start_at_kst  TEXT NOT NULL,
    match_date    TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    imported_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_date
    ON matches(match_date);
CREATE INDEX IF NOT EXISTS idx_matches_teams_date
    ON matches(match_date, team_a, team_b);
"""

SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_events (
    source          TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    match_id        TEXT NOT NULL UNIQUE,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    PRIMARY KEY (source, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_schedule_events_match
    ON schedule_events(match_id);

CREATE TABLE IF NOT EXISTS schedule_sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_key      TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    scanned_count   INTEGER NOT NULL DEFAULT 0,
    eligible_count  INTEGER NOT NULL DEFAULT 0,
    adopted_count   INTEGER NOT NULL DEFAULT 0,
    inserted_count  INTEGER NOT NULL DEFAULT 0,
    updated_count   INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0,
    conflict_count  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    error           TEXT
);
"""

SUMMARY_RE = re.compile(
    r"^(?:\((?P<stage>[^)]+)\)\s*)?"
    r"(?:(?P<round>.*?):\s*)?"
    r"(?P<a>.+?)\s+vs\.?\s+(?P<b>.+?)$",
    re.IGNORECASE,
)
LEGACY_MATCHUP_RE = re.compile(
    r"(?P<a>[A-Za-z0-9][A-Za-z0-9 .'-]*?)\s+vs\.?\s+"
    r"(?P<b>[A-Za-z0-9][A-Za-z0-9 .'-]*?)\s*$",
    re.IGNORECASE,
)
TEAM_ALIASES = {
    "DPLUS KIA": "DK",
    "GEN.G": "GEN",
    "HANWHA LIFE ESPORTS": "HLE",
    "KARMINE CORP": "KC",
    "MOVISTAR KOI": "MKOI",
    "TEAM LIQUID": "TLAW",
    "TL": "TLAW",
    "LY": "LYON",
    "TOP ESPORTS": "TES",
}
UNKNOWN_TEAMS = {"TBD", "???", "TBA", "BYE", ""}


@dataclass(frozen=True)
class ScheduleEvent:
    source_id: str
    stage: str
    team_a: str
    team_b: str
    start_at_kst: datetime

    @property
    def match_date(self) -> str:
        return self.start_at_kst.date().isoformat()

    @property
    def start_iso(self) -> str:
        return self.start_at_kst.isoformat(timespec="seconds")

    @property
    def comparison_key(self) -> tuple[str, tuple[str, str]]:
        return self.match_date, tuple(sorted((self.team_a, self.team_b)))


@dataclass
class ParseStats:
    scanned: int = 0
    out_of_scope: int = 0
    unresolved: int = 0
    malformed: int = 0

    @property
    def skipped(self) -> int:
        return self.out_of_scope + self.unresolved + self.malformed


@dataclass
class SyncStats:
    scanned: int = 0
    eligible: int = 0
    adopted: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    conflicts: int = 0


@dataclass(frozen=True)
class TimeMismatch:
    key: tuple[str, tuple[str, str]]
    feed_time: str
    legacy_time: str


@dataclass
class ComparisonResult:
    matched: int
    missing_from_feed: set[tuple[str, tuple[str, str]]]
    additional_in_feed: set[tuple[str, tuple[str, str]]]
    time_mismatches: list[TimeMismatch]

    @property
    def compatible(self) -> bool:
        return not self.missing_from_feed and not self.time_mismatches


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def unfold_ical_lines(text: str) -> list[str]:
    unfolded: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)
    return unfolded


def unescape_ical_text(value: str) -> str:
    return (
        value.replace("\\N", "\n")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def parse_ical_components(text: str) -> list[dict[str, tuple[dict[str, str], str]]]:
    lines = unfold_ical_lines(text.strip())
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise ValueError("완전한 iCalendar 응답이 아닙니다")
    components: list[dict[str, tuple[dict[str, str], str]]] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise ValueError("이벤트 구조가 잘못되었습니다")
            current = {}
            continue
        if line == "END:VEVENT":
            if current is None:
                raise ValueError("이벤트 구조가 잘못되었습니다")
            components.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        raw_name, value = line.split(":", 1)
        name_parts = raw_name.split(";")
        name = name_parts[0].upper()
        params: dict[str, str] = {}
        for part in name_parts[1:]:
            if "=" in part:
                key, param_value = part.split("=", 1)
                params[key.upper()] = param_value.strip('"')
        current[name] = (params, unescape_ical_text(value))
    if current is not None:
        raise ValueError("이벤트가 끝나지 않았습니다")
    return components


def parse_ical_datetime(params: dict[str, str], value: str) -> datetime:
    if re.fullmatch(r"\d{8}T\d{6}Z", value):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        ).astimezone(KST)
    if re.fullmatch(r"\d{8}T\d{6}", value):
        tzid = params.get("TZID")
        if not tzid:
            raise ValueError("시간대 없는 DTSTART는 지원하지 않습니다")
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(
            tzinfo=ZoneInfo(tzid)
        ).astimezone(KST)
    raise ValueError("지원하지 않는 DTSTART 형식입니다")


def normalize_team(raw: str) -> str:
    normalized = re.sub(r"\s+", " ", raw).strip().upper()
    return TEAM_ALIASES.get(normalized, normalized)


def parse_summary(summary: str) -> tuple[str, str, str] | None:
    match = SUMMARY_RE.match(summary.strip())
    if not match:
        return None
    team_a = normalize_team(match.group("a"))
    team_b = normalize_team(match.group("b"))
    if any(
        team not in UNKNOWN_TEAMS and not re.fullmatch(r"[A-Z0-9]{1,12}", team)
        for team in (team_a, team_b)
    ):
        return None
    stage_parts = [match.group("stage") or "", match.group("round") or ""]
    stage = " ".join(part.strip() for part in stage_parts if part.strip())
    return stage or "일정", team_a, team_b


def in_date_range(
    event_date: date,
    from_date: date | None,
    to_date: date | None,
) -> bool:
    if from_date and event_date < from_date:
        return False
    if to_date and event_date > to_date:
        return False
    return True


def normalize_feed(
    text: str,
    season: int,
    from_date: date | None = None,
    to_date: date | None = None,
) -> tuple[list[ScheduleEvent], ParseStats]:
    events: list[ScheduleEvent] = []
    stats = ParseStats()
    seen_ids: set[str] = set()
    for component in parse_ical_components(text):
        stats.scanned += 1
        try:
            source_id = component["UID"][1].strip()
            start = parse_ical_datetime(*component["DTSTART"])
            parsed = parse_summary(component["SUMMARY"][1])
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            stats.malformed += 1
            continue
        if not source_id or parsed is None or source_id in seen_ids:
            stats.malformed += 1
            continue
        if start.year != season or not in_date_range(start.date(), from_date, to_date):
            stats.out_of_scope += 1
            continue
        stage, team_a, team_b = parsed
        if team_a in UNKNOWN_TEAMS or team_b in UNKNOWN_TEAMS:
            stats.unresolved += 1
            continue
        seen_ids.add(source_id)
        events.append(ScheduleEvent(source_id, stage, team_a, team_b, start))
    events.sort(key=lambda event: (event.start_at_kst, event.source_id))
    return events, stats


def parse_legacy_subject(subject: str) -> tuple[str, str, str] | None:
    normalized = subject.strip()
    prefix, separator, matchup = normalized.rpartition(" - ")
    if not separator:
        prefix, matchup = "", normalized
    match = LEGACY_MATCHUP_RE.fullmatch(matchup)
    if not match:
        return None
    team_a = normalize_team(match.group("a"))
    team_b = normalize_team(match.group("b"))
    stage = prefix.strip() or "일정"
    return stage, team_a, team_b


def read_legacy_csv(
    path: Path,
    season: int,
    from_date: date | None = None,
    to_date: date | None = None,
) -> tuple[list[ScheduleEvent], ParseStats]:
    events: list[ScheduleEvent] = []
    stats = ParseStats()
    with path.open(newline="", encoding="utf-8-sig") as file:
        for index, row in enumerate(csv.DictReader(file), start=2):
            stats.scanned += 1
            parsed = parse_legacy_subject(row.get("Subject", ""))
            try:
                start = datetime.strptime(
                    f"{row.get('Start Date', '').strip()} "
                    f"{row.get('Start Time', '').strip() or '00:00'}",
                    "%Y-%m-%d %H:%M",
                ).replace(tzinfo=KST)
            except ValueError:
                stats.malformed += 1
                continue
            if parsed is None:
                stats.malformed += 1
                continue
            if start.year != season or not in_date_range(start.date(), from_date, to_date):
                stats.out_of_scope += 1
                continue
            stage, team_a, team_b = parsed
            if team_a in UNKNOWN_TEAMS or team_b in UNKNOWN_TEAMS:
                stats.unresolved += 1
                continue
            events.append(
                ScheduleEvent(f"legacy:{index}", stage, team_a, team_b, start)
            )
    events.sort(key=lambda event: (event.start_at_kst, event.source_id))
    return events, stats


def compare_events(
    feed_events: list[ScheduleEvent],
    legacy_events: list[ScheduleEvent],
    tolerance_minutes: int = 0,
) -> ComparisonResult:
    feed_by_key: dict[tuple[str, tuple[str, str]], list[ScheduleEvent]] = defaultdict(list)
    legacy_by_key: dict[tuple[str, tuple[str, str]], list[ScheduleEvent]] = defaultdict(list)
    for event in feed_events:
        feed_by_key[event.comparison_key].append(event)
    for event in legacy_events:
        legacy_by_key[event.comparison_key].append(event)

    feed_keys = set(feed_by_key)
    legacy_keys = set(legacy_by_key)
    shared_keys = feed_keys & legacy_keys
    mismatches: list[TimeMismatch] = []
    tolerance = timedelta(minutes=tolerance_minutes)
    matched = 0
    for key in sorted(shared_keys):
        feed_rows = sorted(feed_by_key[key], key=lambda event: event.start_at_kst)
        legacy_rows = sorted(legacy_by_key[key], key=lambda event: event.start_at_kst)
        for feed_event, legacy_event in zip(feed_rows, legacy_rows):
            if abs(feed_event.start_at_kst - legacy_event.start_at_kst) > tolerance:
                mismatches.append(
                    TimeMismatch(
                        key,
                        feed_event.start_iso,
                        legacy_event.start_iso,
                    )
                )
            else:
                matched += 1
        if len(feed_rows) != len(legacy_rows):
            if len(feed_rows) < len(legacy_rows):
                legacy_keys.add(key)
                feed_keys.discard(key)
            else:
                feed_keys.add(key)
                legacy_keys.discard(key)

    return ComparisonResult(
        matched=matched,
        missing_from_feed=legacy_keys - feed_keys,
        additional_in_feed=feed_keys - legacy_keys,
        time_mismatches=mismatches,
    )


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    initialize_db(connection)
    return connection


def initialize_db(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(MATCHES_SCHEMA)
    connection.executescript(SYNC_SCHEMA)
    connection.commit()


def open_simulation_db(path: Path) -> sqlite3.Connection:
    simulation = sqlite3.connect(":memory:")
    if path.exists():
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            source.backup(simulation)
        finally:
            source.close()
    initialize_db(simulation)
    return simulation


def make_external_match_id(
    source: str,
    season: int,
    source_event_id: str,
) -> str:
    digest = hashlib.sha1(f"{source}:{source_event_id}".encode()).hexdigest()[:12]
    return f"worlds-{season}-{digest}"


def find_unmapped_legacy_match(
    connection: sqlite3.Connection,
    league: str,
    season: int,
    event: ScheduleEvent,
) -> list[str]:
    rows = connection.execute(
        """
        SELECT m.match_id
          FROM matches m
          LEFT JOIN schedule_events se ON se.match_id = m.match_id
         WHERE se.match_id IS NULL
           AND m.league = ?
           AND m.season = ?
           AND m.match_date = ?
           AND ((m.team_a = ? AND m.team_b = ?)
             OR (m.team_a = ? AND m.team_b = ?))
        """,
        (
            league,
            season,
            event.match_date,
            event.team_a,
            event.team_b,
            event.team_b,
            event.team_a,
        ),
    ).fetchall()
    return [row[0] for row in rows]


def upsert_schedule(
    connection: sqlite3.Connection,
    events: list[ScheduleEvent],
    source: str,
    league: str,
    season: int,
    skipped_count: int = 0,
) -> SyncStats:
    now = utc_now()
    stats = SyncStats(
        scanned=len(events) + skipped_count,
        eligible=len(events),
        skipped=skipped_count,
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        for event in events:
            mapping = connection.execute(
                """
                SELECT match_id FROM schedule_events
                 WHERE source = ? AND source_event_id = ?
                """,
                (source, event.source_id),
            ).fetchone()
            if mapping:
                match_id = mapping[0]
            else:
                legacy_matches = find_unmapped_legacy_match(
                    connection, league, season, event
                )
                if len(legacy_matches) > 1:
                    stats.conflicts += 1
                    continue
                if legacy_matches:
                    match_id = legacy_matches[0]
                    stats.adopted += 1
                else:
                    match_id = make_external_match_id(
                        source, season, event.source_id
                    )

            existing = connection.execute(
                """
                SELECT league, season, stage, team_a, team_b, start_at_kst,
                       match_date, source_file
                  FROM matches WHERE match_id = ?
                """,
                (match_id,),
            ).fetchone()
            values = (
                league,
                season,
                event.stage,
                event.team_a,
                event.team_b,
                event.start_iso,
                event.match_date,
                f"ical:{source}",
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO matches
                      (match_id, league, season, stage, team_a, team_b,
                       start_at_kst, match_date, source_file, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (match_id, *values, now),
                )
                stats.inserted += 1
            elif tuple(existing) != values:
                connection.execute(
                    """
                    UPDATE matches
                       SET league = ?, season = ?, stage = ?, team_a = ?,
                           team_b = ?, start_at_kst = ?, match_date = ?,
                           source_file = ?, imported_at = ?
                     WHERE match_id = ?
                    """,
                    (*values, now, match_id),
                )
                stats.updated += 1
            else:
                stats.unchanged += 1

            connection.execute(
                """
                INSERT INTO schedule_events
                  (source, source_event_id, match_id, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, source_event_id) DO UPDATE
                  SET match_id = excluded.match_id,
                      last_seen = excluded.last_seen
                """,
                (source, event.source_id, match_id, now, now),
            )
        if stats.conflicts:
            connection.rollback()
            stats.adopted = stats.inserted = stats.updated = stats.unchanged = 0
        else:
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    return stats


def start_run(
    connection: sqlite3.Connection,
    source: str,
    source_key: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO schedule_sync_runs(source, source_key, started_at, status)
        VALUES (?, ?, ?, 'running')
        """,
        (source, source_key, utc_now()),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    status: str,
    stats: SyncStats | None = None,
    error: str | None = None,
) -> None:
    stats = stats or SyncStats()
    connection.execute(
        """
        UPDATE schedule_sync_runs
           SET finished_at = ?, scanned_count = ?, eligible_count = ?,
               adopted_count = ?, inserted_count = ?, updated_count = ?,
               unchanged_count = ?, skipped_count = ?, conflict_count = ?,
               status = ?, error = ?
         WHERE id = ?
        """,
        (
            utc_now(),
            stats.scanned,
            stats.eligible,
            stats.adopted,
            stats.inserted,
            stats.updated,
            stats.unchanged,
            stats.skipped,
            stats.conflicts,
            status,
            error,
            run_id,
        ),
    )
    connection.commit()


def fetch_feed(url: str) -> str:
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx가 필요합니다. requirements.txt를 설치하세요") from exc

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=30,
            headers={"User-Agent": "lck-nospo-schedule-sync/1.0"},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            content = response.content
    except httpx.HTTPError as exc:
        raise RuntimeError("일정 피드를 가져오지 못했습니다") from exc
    if len(content) > MAX_FEED_BYTES:
        raise ValueError("일정 피드가 허용 크기를 초과했습니다")
    return content.decode("utf-8-sig")


def log_comparison(result: ComparisonResult, verbose: bool) -> None:
    logger.info(
        "비교 완료: 일치 %d / 피드 누락 %d / 피드 추가 %d / 시각 차이 %d",
        result.matched,
        len(result.missing_from_feed),
        len(result.additional_in_feed),
        len(result.time_mismatches),
    )
    if not verbose:
        return
    for match_date, teams in sorted(result.missing_from_feed):
        logger.debug("피드 누락: %s %s vs %s", match_date, *teams)
    for match_date, teams in sorted(result.additional_in_feed):
        logger.debug("피드 추가: %s %s vs %s", match_date, *teams)
    for mismatch in result.time_mismatches:
        match_date, teams = mismatch.key
        logger.debug(
            "시각 차이: %s %s vs %s / 피드 %s / 기존 %s",
            match_date,
            *teams,
            mismatch.feed_time,
            mismatch.legacy_time,
        )


def parse_cli_date(raw: str | None) -> date | None:
    return date.fromisoformat(raw) if raw else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="구독형 iCalendar 일정을 검증하고 matches에 동기화",
    )
    parser.add_argument("--season", type=int, required=True, help="대회 연도")
    parser.add_argument("--league", default="WORLDS", help="저장할 리그 코드")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="일정 소스 코드")
    parser.add_argument("--url", default=DEFAULT_URL, help="구독형 iCalendar URL")
    parser.add_argument("--feed-file", type=Path, help="URL 대신 읽을 로컬 iCalendar")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite 경로")
    parser.add_argument("--compare-csv", type=Path, help="비교할 Leaguepedia CSV")
    parser.add_argument("--from-date", help="비교/동기화 시작일 YYYY-MM-DD")
    parser.add_argument("--to-date", help="비교/동기화 종료일 YYYY-MM-DD")
    parser.add_argument(
        "--time-tolerance-minutes",
        type=int,
        default=0,
        help="비교 시 허용할 시작 시각 차이",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="검증 결과를 실제 DB에 반영",
    )
    parser.add_argument("--verbose", action="store_true", help="매치업별 차이 출력")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    configure_logging(args.verbose)
    connection: sqlite3.Connection | None = None
    run_id: int | None = None
    try:
        from_date = parse_cli_date(args.from_date)
        to_date = parse_cli_date(args.to_date)
        if from_date and to_date and from_date > to_date:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다")
        if args.time_tolerance_minutes < 0:
            raise ValueError("시각 허용 오차는 0 이상이어야 합니다")
        source_key = str(args.feed_file) if args.feed_file else args.url
        if args.apply:
            connection = open_db(args.db)
            run_id = start_run(connection, args.source, source_key)
        if args.feed_file:
            feed_text = args.feed_file.read_text(encoding="utf-8-sig")
        else:
            feed_text = fetch_feed(args.url)
        events, parse_stats = normalize_feed(
            feed_text,
            args.season,
            from_date,
            to_date,
        )
        if parse_stats.malformed:
            raise ValueError(
                f"피드 이벤트 {parse_stats.malformed}건을 파싱하지 못했습니다"
            )
        if not events:
            raise ValueError("조건에 맞는 확정 매치업이 없습니다")
        logger.info(
            "피드 검증: 전체 %d / 동기화 대상 %d / 범위 밖 %d / 미정 %d / 파싱 실패 %d",
            parse_stats.scanned,
            len(events),
            parse_stats.out_of_scope,
            parse_stats.unresolved,
            parse_stats.malformed,
        )

        if args.compare_csv:
            legacy_events, legacy_stats = read_legacy_csv(
                args.compare_csv,
                args.season,
                from_date,
                to_date,
            )
            if legacy_stats.malformed:
                raise ValueError(
                    f"기존 CSV {legacy_stats.malformed}건을 파싱하지 못했습니다"
                )
            if not legacy_events:
                raise ValueError("비교 범위에 기존 확정 경기가 없습니다")
            result = compare_events(
                events,
                legacy_events,
                args.time_tolerance_minutes,
            )
            log_comparison(result, args.verbose)
            if not result.compatible:
                logger.error("비교 검증 실패: DB 반영을 중단합니다")
                if connection is not None and run_id is not None:
                    finish_run(connection, run_id, "rejected", error="기존 CSV 비교 불일치")
                return 2

        if not args.apply:
            simulation = open_simulation_db(args.db)
            try:
                sync_stats = upsert_schedule(
                    simulation,
                    events,
                    args.source,
                    args.league,
                    args.season,
                    parse_stats.skipped,
                )
            finally:
                simulation.close()
            logger.info(
                "드라이런 결과: 기존 연결 %d / 신규 %d / 업데이트 %d / "
                "변경 없음 %d / 충돌 %d",
                sync_stats.adopted,
                sync_stats.inserted,
                sync_stats.updated,
                sync_stats.unchanged,
                sync_stats.conflicts,
            )
            logger.info("DB는 변경하지 않았습니다. --apply를 지정하면 반영합니다")
            return 0 if sync_stats.conflicts == 0 else 3

        assert connection is not None and run_id is not None
        sync_stats = upsert_schedule(
            connection,
            events,
            args.source,
            args.league,
            args.season,
            parse_stats.skipped,
        )
        status = "success" if sync_stats.conflicts == 0 else "rejected"
        finish_run(connection, run_id, status, sync_stats)
        logger.info(
            "DB 반영 완료: 기존 연결 %d / 신규 %d / 업데이트 %d / 변경 없음 %d / 충돌 %d",
            sync_stats.adopted,
            sync_stats.inserted,
            sync_stats.updated,
            sync_stats.unchanged,
            sync_stats.conflicts,
        )
        return 0 if sync_stats.conflicts == 0 else 3
    except Exception as exc:
        error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        if connection is not None and run_id is not None:
            connection.rollback()
            finish_run(connection, run_id, "failed", error=error)
        logger.error("일정 동기화 실패: %s", error)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
