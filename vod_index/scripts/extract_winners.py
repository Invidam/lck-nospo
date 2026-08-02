#!/usr/bin/env python3
"""videos 테이블의 인터뷰 영상 제목에서 승팀을 추출해 match_winners에 적재.

치지직 LCK 채널의 MVP/VIP 인터뷰 영상은 다음 패턴으로 올라온다.

    <승팀약어> <선수닉> 인터뷰 | <팀A> vs <팀B> | 2026 LCK

첫 토큰의 팀 약어가 그 매치의 승팀이다 (현재 시드 데이터 23/23 정합).
같은 매치에 인터뷰가 여러 개여도 승팀은 동일하므로 (source, source_id)별로
행을 적재한다 — 중복 검출이 무해하고 추후 신뢰도 보강에도 쓸 수 있음.

publish_date는 KST 자정 기준 날짜로 변환해 저장. 실제 경기일과 ±1일
차이 가능하므로 봇 워커 쪽에서 윈도우로 흡수한다.

사용법:
    python vod_index/scripts/extract_winners.py             # 전체 재추출
    python vod_index/scripts/extract_winners.py --only-new  # 미처리만
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.environ["LCK_DB_PATH"]) if os.environ.get("LCK_DB_PATH") else ROOT / "data" / "vods" / "videos.sqlite"
KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS match_winners (
    match_date  TEXT NOT NULL,
    team_a      TEXT NOT NULL,
    team_b      TEXT NOT NULL,
    winner      TEXT NOT NULL,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (match_date, team_a, team_b, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_mw_lookup
    ON match_winners(match_date, team_a, team_b, winner);

CREATE TABLE IF NOT EXISTS winners_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mode           TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    scanned_count  INTEGER,
    matched_count  INTEGER,
    skipped_count  INTEGER,
    inserted_count INTEGER,
    status         TEXT NOT NULL,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_winners_runs_lookup
    ON winners_runs(started_at DESC);
"""

INTERVIEW = re.compile(
    r"^(?P<winner>[A-Z0-9]+)\s+\S+\s+인터뷰\s*[ㅣ|]\s*"
    r"(?P<a>[A-Z0-9]+)\s+vs\s+(?P<b>[A-Z0-9]+)"
)


def extract(title: str) -> dict | None:
    m = INTERVIEW.match(title.strip())
    if not m:
        return None
    winner, a, b = m.group("winner"), m.group("a"), m.group("b")
    if winner not in (a, b):
        return None
    return {"winner": winner, "team_a": a, "team_b": b}


def kst_date(publish_date: str) -> str | None:
    try:
        dt = datetime.strptime(publish_date, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(publish_date, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    return dt.strftime("%Y-%m-%d")


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def run(conn: sqlite3.Connection, only_new: bool) -> tuple[int, int, int, int]:
    if only_new:
        q = """
            SELECT v.source, v.source_id, v.title, v.publish_date
              FROM videos v
              LEFT JOIN match_winners w
                ON v.source = w.source AND v.source_id = w.source_id
             WHERE w.source_id IS NULL
               AND v.title LIKE '%인터뷰%'
        """
    else:
        q = """
            SELECT source, source_id, title, publish_date
              FROM videos
             WHERE title LIKE '%인터뷰%'
        """

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scanned = 0
    matched = 0
    skipped = 0
    inserted = 0
    for source, source_id, title, publish_date in conn.execute(q):
        scanned += 1
        result = extract(title)
        if not result:
            skipped += 1
            continue
        match_date = kst_date(publish_date)
        if not match_date:
            skipped += 1
            continue
        matched += 1
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO match_winners
              (match_date, team_a, team_b, winner, source, source_id, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (match_date, result["team_a"], result["team_b"],
             result["winner"], source, source_id, now_iso),
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return scanned, matched, skipped, inserted


def record_run_start(conn: sqlite3.Connection, mode: str) -> int:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        """
        INSERT INTO winners_runs (mode, started_at, status)
        VALUES (?, ?, 'running')
        """,
        (mode, started),
    )
    conn.commit()
    return cur.lastrowid


def record_run_finish(
    conn: sqlite3.Connection,
    run_id: int,
    scanned: int,
    matched: int,
    skipped: int,
    inserted: int,
    status: str,
    error: str | None,
) -> None:
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        UPDATE winners_runs
           SET finished_at = ?, scanned_count = ?, matched_count = ?,
               skipped_count = ?, inserted_count = ?, status = ?, error = ?
         WHERE id = ?
        """,
        (finished, scanned, matched, skipped, inserted, status, error, run_id),
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="인터뷰 영상 → 승팀 추출 적재")
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help="SQLite 경로 (기본: data/vods/videos.sqlite)")
    parser.add_argument("--only-new", action="store_true",
                        help="아직 처리 안 된 인터뷰 영상만")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"DB 없음: {args.db}", file=sys.stderr)
        return 1

    conn = open_db(args.db)
    mode = "only_new" if args.only_new else "full"
    run_id = record_run_start(conn, mode)
    try:
        scanned, matched, skipped, inserted = run(conn, args.only_new)
    except Exception as e:
        record_run_finish(conn, run_id, 0, 0, 0, 0, "failed", str(e))
        print(f"승팀 추출 실패: {e}", file=sys.stderr)
        return 1

    record_run_finish(conn, run_id, scanned, matched, skipped, inserted, "ok", None)
    print(
        f"완료: 스캔 {scanned}건 / 매칭 {matched}건 / 스킵 {skipped}건 / 신규 적재 {inserted}건"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
