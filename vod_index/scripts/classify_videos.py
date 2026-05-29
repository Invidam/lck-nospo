#!/usr/bin/env python3
"""videos 테이블의 영상 제목을 파싱해 video_classifications에 분류 결과 적재.

원본 videos 테이블은 건드리지 않는다 (소스 어댑터 책임 분리).
이 분류 결과는 (match_date + team_a + team_b + set_index + kind) 조합으로
봇이 사용자 입력과 매칭한다.

분류 종류 (kind):
  full           — N세트 풀VOD (제목에 "게임 N VOD")
  set_highlight  — N세트 하이라이트 (제목에 "게임 N 하이라이트")
  match_full     — 매치 풀VOD ("매치 N VOD"; 현재 데이터엔 거의 없음)
  match_highlight— 매치 전체 종합 하이라이트 (스포 위험 ↑, 봇 노출 보류 권장)
  excluded       — 인터뷰/예고/이벤트/솔랭콘텐츠 등 (다시보기 대상 아님)

매치 날짜는 영상 publish_date에서 KST로 변환해 추출. 실제 경기일과 1일
차이날 수 있으므로 (date, date-1, date-2) 윈도우로 매치 매핑.

사용법:
    python vod_index/scripts/classify_videos.py             # 전체 재분류
    python vod_index/scripts/classify_videos.py --only-new  # 미분류 영상만
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "data" / "vods" / "videos.sqlite"
KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_classifications (
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    team_a       TEXT,
    team_b       TEXT,
    set_index    INTEGER,
    match_date   TEXT,
    classified_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id),
    FOREIGN KEY (source, source_id) REFERENCES videos(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_vc_match_lookup
    ON video_classifications(match_date, team_a, team_b, set_index, kind);

CREATE TABLE IF NOT EXISTS classify_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mode             TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    scanned_count    INTEGER,
    classified_count INTEGER,
    excluded_count   INTEGER,
    status           TEXT NOT NULL,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_classify_runs_lookup
    ON classify_runs(started_at DESC);
"""

GAME_FULL = re.compile(
    r"(?P<a>[A-Z0-9]+)\s+vs\s+(?P<b>[A-Z0-9]+)\s+게임\s*(?P<set>\d+)\s+VOD"
)
GAME_HIGHLIGHT = re.compile(
    r"(?P<a>[A-Z0-9]+)\s+vs\s+(?P<b>[A-Z0-9]+)\s+게임\s*(?P<set>\d+)\s+하이라이트"
)
MATCH_FULL = re.compile(
    r"(?P<a>[A-Z0-9]+)\s+vs\s+(?P<b>[A-Z0-9]+)\s+매치\s*\d+\s+VOD"
)
MATCH_HIGHLIGHT = re.compile(
    r"(?P<a>[A-Z0-9]+)\s+vs\s+(?P<b>[A-Z0-9]+)\s+매치\s*\d+\s+하이라이트"
)
INTERVIEW = re.compile(r"인터뷰")
SHOWDOWN = re.compile(r"Showdown|Saturday|Sunday|프리뷰|미디어")
PREVIEW = re.compile(
    r"^[A-Z0-9]+\s+vs\s+[A-Z0-9]+\s+-\s+[A-Z0-9]+\s+vs\s+[A-Z0-9]+"
)


def classify(title: str) -> dict | None:
    """제목 → 분류 dict. 다시보기 대상 아니면 kind='excluded'."""
    if INTERVIEW.search(title):
        return {"kind": "excluded"}
    if SHOWDOWN.search(title):
        return {"kind": "excluded"}
    if PREVIEW.match(title.strip()):
        return {"kind": "excluded"}

    m = GAME_FULL.search(title)
    if m:
        return {"kind": "full", "team_a": m.group("a"), "team_b": m.group("b"),
                "set_index": int(m.group("set"))}
    m = GAME_HIGHLIGHT.search(title)
    if m:
        return {"kind": "set_highlight", "team_a": m.group("a"), "team_b": m.group("b"),
                "set_index": int(m.group("set"))}
    m = MATCH_FULL.search(title)
    if m:
        return {"kind": "match_full", "team_a": m.group("a"), "team_b": m.group("b"),
                "set_index": None}
    m = MATCH_HIGHLIGHT.search(title)
    if m:
        return {"kind": "match_highlight", "team_a": m.group("a"), "team_b": m.group("b"),
                "set_index": None}

    return {"kind": "excluded"}


def kst_date(publish_date: str) -> str | None:
    """videos.publish_date는 'YYYY-MM-DD HH:MM:SS' (치지직은 이미 KST로 보임).

    KST 자정 기준 날짜를 그대로 사용. 만약 publish가 경기 종료 자정 직후라면
    실제 경기일과 1일 차이날 수 있는데, 그건 match 매칭 쿼리에서 ±1 윈도우로 흡수.
    """
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


def run(conn: sqlite3.Connection, only_new: bool) -> tuple[int, int, int]:
    if only_new:
        q = """
            SELECT v.source, v.source_id, v.title, v.publish_date
              FROM videos v
              LEFT JOIN video_classifications c
                ON v.source = c.source AND v.source_id = c.source_id
             WHERE c.source IS NULL
        """
    else:
        q = "SELECT source, source_id, title, publish_date FROM videos"

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scanned = 0
    classified = 0
    excluded = 0
    for source, source_id, title, publish_date in conn.execute(q):
        scanned += 1
        result = classify(title)
        if result is None:
            continue
        match_date = kst_date(publish_date) if result["kind"] != "excluded" else None
        conn.execute(
            """
            INSERT OR REPLACE INTO video_classifications
              (source, source_id, kind, team_a, team_b, set_index,
               match_date, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, source_id, result["kind"],
             result.get("team_a"), result.get("team_b"),
             result.get("set_index"), match_date, now_iso),
        )
        if result["kind"] == "excluded":
            excluded += 1
        else:
            classified += 1
    conn.commit()
    return scanned, classified, excluded


def record_run_start(conn: sqlite3.Connection, mode: str) -> int:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        """
        INSERT INTO classify_runs (mode, started_at, status)
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
    classified: int,
    excluded: int,
    status: str,
    error: str | None,
) -> None:
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        UPDATE classify_runs
           SET finished_at = ?, scanned_count = ?, classified_count = ?,
               excluded_count = ?, status = ?, error = ?
         WHERE id = ?
        """,
        (finished, scanned, classified, excluded, status, error, run_id),
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="영상 제목 파싱 → 분류 적재")
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help=f"SQLite 경로 (기본: {DB_PATH.relative_to(ROOT)})")
    parser.add_argument("--only-new", action="store_true",
                        help="미분류 영상만 처리")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"DB 없음: {args.db}", file=sys.stderr)
        return 1

    conn = open_db(args.db)
    mode = "only_new" if args.only_new else "full"
    run_id = record_run_start(conn, mode)
    try:
        scanned, classified, excluded = run(conn, args.only_new)
    except Exception as e:
        record_run_finish(conn, run_id, 0, 0, 0, "failed", str(e))
        print(f"분류 실패: {e}", file=sys.stderr)
        return 1

    record_run_finish(conn, run_id, scanned, classified, excluded, "ok", None)
    print(f"완료: 스캔 {scanned}건 / 분류 {classified}건 / 제외 {excluded}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
