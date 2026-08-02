#!/usr/bin/env python3
"""치지직 채널의 VOD 목록을 주기적으로 수집해 공용 SQLite(`videos`)에 누적 저장한다.

DB 스키마는 소스 중립이다(`source`, `source_id`, `url`, `title`, `publish_date`).
이 스크립트는 치지직 API → 공용 스키마로 변환해 넣는 어댑터일 뿐이고,
DB 자체는 유튜브 / SOOP 등 다른 소스가 같은 테이블에 추가로 적재할 수 있다.

저장 데이터는 결과 유추 가능 필드(영상 제목)를 포함하므로 **backend 전용**이다.
프론트엔드로 그대로 흘리지 말 것. (AGENTS.md §3 참고)

기본 동작은 증분 수집이다. DB에서 (source='chzzk', source_key=channel_id) 기준
가장 최신 publish_date 이후 영상만 가져온다. cron / loop으로 주기 실행하면
신규 VOD만 누적된다.

사용법:
    # 증분 수집 (DB의 최신 게시일 이후만)
    python vod_index/scripts/fetch_chzzk_vods.py <CHANNEL_ID>

    # 전체 재수집
    python vod_index/scripts/fetch_chzzk_vods.py <CHANNEL_ID> --full

    # 특정 날짜 이후로 컷오프
    python vod_index/scripts/fetch_chzzk_vods.py <CHANNEL_ID> --since 2026-05-01

예:
    # LCK 공식 채널 증분 수집
    python vod_index/scripts/fetch_chzzk_vods.py 9381e7d6816e6d915a44a13c0195b202

출력: data/vods/videos.sqlite
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.environ["LCK_DB_PATH"]) if os.environ.get("LCK_DB_PATH") else ROOT / "data" / "vods" / "videos.sqlite"


def _rel(p: Path) -> str:
    """ROOT 기준 상대경로. 볼륨 등 ROOT 밖이면 절대경로 그대로."""
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)

SOURCE = "chzzk"
API_BASE = "https://api.chzzk.naver.com/service/v1/channels"
VIDEO_URL_BASE = "https://chzzk.naver.com/video"
PAGE_SIZE = 18
SLEEP_SEC = 0.3
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    publish_date TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_videos_publish
    ON videos(source, publish_date DESC);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_key    TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    new_count     INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    cutoff        TEXT,
    status        TEXT NOT NULL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_runs_lookup
    ON fetch_runs(source, source_key, started_at DESC);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def fetch_page(channel_id: str, page: int, size: int = PAGE_SIZE) -> dict:
    params = {
        "sortType": "LATEST",
        "pagingType": "PAGE",
        "page": page,
        "size": size,
        "publishDateAt": "",
        "videoType": "",
    }
    url = f"{API_BASE}/{channel_id}/videos?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT,
            "Origin": "https://chzzk.naver.com",
            "Referer": "https://chzzk.naver.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    if body.get("code") != 200:
        raise RuntimeError(f"chzzk api error: {body.get('code')} {body.get('message')}")
    return body["content"]


def to_video_row(raw: dict) -> dict:
    """치지직 API 응답 한 건 → 공용 스키마 dict."""
    video_no = raw["videoNo"]
    return {
        "source_id": str(video_no),
        "url": f"{VIDEO_URL_BASE}/{video_no}",
        "title": raw["videoTitle"],
        "publish_date": raw["publishDate"],
    }


def latest_publish_date(conn: sqlite3.Connection, source_key: str) -> str | None:
    """공용 테이블 기준으로 채널별 최신 publish_date 조회.

    source_key는 chzzk 채널 ID. videos 테이블엔 채널 컬럼이 없으므로
    fetch_runs를 통해 같은 채널의 가장 최신 수집 시점 이후 게시된 row 중
    publish_date 최댓값을 찾는다 - 라고 하기엔 복잡하니, URL로 구분한다:
    chzzk 채널 ID는 URL에 안 들어가므로 채널 단위 latest는 fetch_runs에
    기록된 직전 cutoff를 신뢰한다. 단순화: 같은 source 전체 최댓값을 쓴다.

    같은 DB에 채널 여러 개를 넣을 거면 이 함수는 채널별로 구분되지 않음 →
    필요해지면 videos에 source_key 컬럼을 추가하는 마이그레이션이 필요.
    """
    row = conn.execute(
        "SELECT MAX(publish_date) FROM videos WHERE source = ?",
        (SOURCE,),
    ).fetchone()
    return row[0] if row and row[0] else None


def upsert(
    conn: sqlite3.Connection,
    rows: list[dict],
    now_iso: str,
) -> tuple[int, int]:
    new_count = 0
    updated_count = 0
    for r in rows:
        cur = conn.execute(
            "SELECT 1 FROM videos WHERE source = ? AND source_id = ?",
            (SOURCE, r["source_id"]),
        ).fetchone()
        if cur is None:
            conn.execute(
                """
                INSERT INTO videos
                  (source, source_id, url, title, publish_date, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (SOURCE, r["source_id"], r["url"], r["title"], r["publish_date"],
                 now_iso, now_iso),
            )
            new_count += 1
        else:
            conn.execute(
                """
                UPDATE videos
                   SET url = ?, title = ?, publish_date = ?, last_seen = ?
                 WHERE source = ? AND source_id = ?
                """,
                (r["url"], r["title"], r["publish_date"], now_iso,
                 SOURCE, r["source_id"]),
            )
            updated_count += 1
    return new_count, updated_count


def crawl(
    conn: sqlite3.Connection,
    channel_id: str,
    cutoff: str | None,
    max_pages: int | None,
) -> tuple[int, int, int]:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    first = fetch_page(channel_id, page=0)
    total_pages = first["totalPages"]
    total_count = first["totalCount"]
    limit = total_pages if max_pages is None else min(max_pages, total_pages)
    print(
        f"채널 {channel_id}: 총 {total_count}개 VOD, {total_pages}페이지 (cutoff={cutoff})",
        file=sys.stderr,
    )

    pages_fetched = 0
    new_total = 0
    updated_total = 0

    def process(page_videos: list[dict], page_no: int) -> bool:
        nonlocal new_total, updated_total, pages_fetched
        targets_raw = (
            [v for v in page_videos if v["publishDate"] > cutoff]
            if cutoff
            else page_videos
        )
        rows = [to_video_row(v) for v in targets_raw]
        n, u = upsert(conn, rows, now_iso)
        new_total += n
        updated_total += u
        pages_fetched += 1
        print(
            f"  page {page_no + 1}/{limit}: {len(page_videos)}건 중 {len(rows)}건 처리 "
            f"(신규 {n}, 업데이트 {u})",
            file=sys.stderr,
        )
        if cutoff and page_videos:
            oldest = min(v["publishDate"] for v in page_videos)
            if oldest <= cutoff:
                return True
        return False

    if process(first["data"], 0):
        conn.commit()
        return pages_fetched, new_total, updated_total

    for page in range(1, limit):
        time.sleep(SLEEP_SEC)
        content = fetch_page(channel_id, page=page)
        if process(content["data"], page):
            break

    conn.commit()
    return pages_fetched, new_total, updated_total


def record_run_start(conn: sqlite3.Connection, source_key: str, cutoff: str | None) -> int:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        """
        INSERT INTO fetch_runs (source, source_key, started_at, cutoff, status)
        VALUES (?, ?, ?, ?, 'running')
        """,
        (SOURCE, source_key, started, cutoff),
    )
    conn.commit()
    return cur.lastrowid


def record_run_finish(
    conn: sqlite3.Connection,
    run_id: int,
    pages: int,
    new_count: int,
    updated_count: int,
    status: str,
    error: str | None,
) -> None:
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        UPDATE fetch_runs
           SET finished_at = ?, pages_fetched = ?, new_count = ?,
               updated_count = ?, status = ?, error = ?
         WHERE id = ?
        """,
        (finished, pages, new_count, updated_count, status, error, run_id),
    )
    conn.commit()


def _parse_since(s: str) -> str:
    datetime.strptime(s, "%Y-%m-%d")
    return f"{s} 00:00:00"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="치지직 채널 VOD 목록을 SQLite(공용 스키마)에 누적 수집한다 (backend 전용).",
    )
    parser.add_argument("channel_id", help="치지직 채널 ID (32자리 hex)")
    parser.add_argument(
        "--full",
        action="store_true",
        help="DB 상태 무시하고 전체 페이지 재수집 (기본은 증분).",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="YYYY-MM-DD. 명시적 컷오프. --full / 증분 기본보다 우선.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="가져올 최대 페이지 수 (디버그용).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"SQLite DB 경로 (기본: {_rel(DB_PATH)})",
    )
    args = parser.parse_args()

    conn = open_db(args.db)

    if args.since:
        cutoff = args.since
    elif args.full:
        cutoff = None
    else:
        cutoff = latest_publish_date(conn, args.channel_id)

    run_id = record_run_start(conn, args.channel_id, cutoff)
    try:
        pages, new_count, updated_count = crawl(
            conn, args.channel_id, cutoff, args.max_pages
        )
    except Exception as e:
        record_run_finish(conn, run_id, 0, 0, 0, "failed", str(e))
        print(f"수집 실패: {e}", file=sys.stderr)
        return 1

    record_run_finish(conn, run_id, pages, new_count, updated_count, "ok", None)
    print(
        f"완료: {pages}페이지 / 신규 {new_count}건 / 업데이트 {updated_count}건 "
        f"→ {_rel(args.db)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
