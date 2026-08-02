#!/usr/bin/env python3
"""data/schedules/*.ics(실제는 CSV) → matches 테이블에 정규화 적재.

DB는 fetch_chzzk_vods.py와 동일한 data/vods/videos.sqlite를 공유한다.
matches 테이블은 (league, stage, team_a, team_b, match_date)로 사용자 입력과
직접 매칭하기 위한 정규화 레이어다.

BO3/BO5는 의도적으로 저장하지 않는다 — 봇은 항상 5세트 슬롯을 그려서
경기 형식 자체로도 결과를 유추할 수 없게 한다 (README 무스포 원칙).

match_date는 KST(Asia/Seoul) 기준 YYYY-MM-DD. 사용자가 "2026-04-01"이라고
치면 그 날짜의 한국 시간 경기들을 찾을 수 있도록.

사용법:
    # 단일 파일
    python schedule/scripts/import_schedule.py data/schedules/rounds-1-2.ics

    # 글롭으로 여러 파일 (팀별 필터링 결과는 제외 권장 - 중복)
    python schedule/scripts/import_schedule.py data/schedules/*.ics

    # 전체 wipe 후 재적재
    python schedule/scripts/import_schedule.py data/schedules/rounds-1-2.ics --reset
"""
from __future__ import annotations

import argparse
import csv
import os
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.environ["LCK_DB_PATH"]) if os.environ.get("LCK_DB_PATH") else ROOT / "data" / "vods" / "videos.sqlite"

SUBJECT_RE = re.compile(
    r"^(?P<league>.+?)\s+(?P<year>\d{4})\s+(?P<stage>.+?)\s+-\s+"
    r"(?P<a>[A-Z0-9]+)\s+vs\s+(?P<b>[A-Z0-9]+)\s*$"
)

# Leaguepedia가 Subject 앞에 박는 리그 prefix → 저장용 약어.
# 신규 대회 들어오면 여기에 한 줄 추가. 매칭 안 되면 스킵 (무성 추가 방지).
# 더 긴 prefix가 먼저 매칭되도록 길이 내림차순으로 검사.
LEAGUE_PREFIXES: list[tuple[str, str]] = [
    ("Esports World Cup", "RTE"),
    ("LCK", "LCK"),
]


def _normalize_league(raw: str) -> str | None:
    raw = raw.strip()
    for prefix, code in sorted(LEAGUE_PREFIXES, key=lambda x: -len(x[0])):
        if raw == prefix:
            return code
    return None

SCHEMA = """
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


def parse_subject(subject: str) -> dict[str, str] | None:
    m = SUBJECT_RE.match(subject.strip())
    if not m:
        return None
    league = _normalize_league(m.group("league"))
    if league is None:
        return None
    return {
        "league": league,
        "season": m.group("year"),
        "stage": m.group("stage").strip(),
        "team_a": m.group("a"),
        "team_b": m.group("b"),
    }


def make_match_id(league: str, season: str, date: str, team_a: str, team_b: str, hhmm: str) -> str:
    teams = "-".join(sorted([team_a, team_b]))
    raw = f"{league}-{season}-{date}-{teams}-{hhmm}".lower()
    digest = hashlib.sha1(raw.encode()).hexdigest()[:8]
    return f"{league.lower()}-{season}-{date}-{teams.lower()}-{digest}"


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def read_rows(source: Path) -> list[dict[str, str]]:
    with source.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def upsert_matches(
    conn: sqlite3.Connection,
    rows: list[dict[str, str]],
    source_file: str,
    now_iso: str,
) -> tuple[int, int, int]:
    inserted = 0
    updated = 0
    skipped = 0
    for row in rows:
        subject = row.get("Subject", "")
        date = row.get("Start Date", "").strip()
        hhmm = row.get("Start Time", "").strip()
        parsed = parse_subject(subject)
        if not parsed or not date:
            skipped += 1
            continue
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            skipped += 1
            continue
        start_at_kst = f"{date}T{hhmm or '00:00'}:00+09:00"
        match_id = make_match_id(
            parsed["league"], parsed["season"], date,
            parsed["team_a"], parsed["team_b"], hhmm or "0000",
        )
        cur = conn.execute(
            "SELECT 1 FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        if cur is None:
            conn.execute(
                """
                INSERT INTO matches
                  (match_id, league, season, stage, team_a, team_b,
                   start_at_kst, match_date, source_file, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (match_id, parsed["league"], int(parsed["season"]), parsed["stage"],
                 parsed["team_a"], parsed["team_b"],
                 start_at_kst, date, source_file, now_iso),
            )
            inserted += 1
        else:
            conn.execute(
                """
                UPDATE matches
                   SET stage = ?, start_at_kst = ?, source_file = ?, imported_at = ?
                 WHERE match_id = ?
                """,
                (parsed["stage"], start_at_kst, source_file, now_iso, match_id),
            )
            updated += 1
    conn.commit()
    return inserted, updated, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="data/schedules/*.ics(CSV) → matches 테이블 적재",
    )
    parser.add_argument("sources", nargs="+", type=Path,
                        help="입력 파일들 (CSV 포맷, .ics 확장자도 OK)")
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help="SQLite 경로 (기본: data/vods/videos.sqlite)")
    parser.add_argument("--reset", action="store_true",
                        help="적재 전에 matches 테이블 전체 삭제")
    args = parser.parse_args()

    missing = [p for p in args.sources if not p.exists()]
    if missing:
        for p in missing:
            print(f"파일 없음: {p}", file=sys.stderr)
        return 1

    conn = open_db(args.db)
    if args.reset:
        conn.execute("DELETE FROM matches")
        conn.commit()
        print("matches 테이블 초기화", file=sys.stderr)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_ins = total_upd = total_skip = 0
    for src in args.sources:
        rows = read_rows(src)
        resolved = src.resolve()
        try:
            rel = resolved.relative_to(ROOT)
        except ValueError:
            rel = src
        ins, upd, skip = upsert_matches(conn, rows, str(rel), now_iso)
        total_ins += ins
        total_upd += upd
        total_skip += skip
        print(f"  {rel}: 신규 {ins} / 업데이트 {upd} / 스킵 {skip}", file=sys.stderr)

    print(f"완료: 신규 {total_ins} / 업데이트 {total_upd} / 스킵 {total_skip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
