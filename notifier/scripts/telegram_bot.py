#!/usr/bin/env python3
"""LCK 무스포 다시보기 텔레그램 봇.

사용자가 날짜 + 팀(1개 또는 2개)을 입력하면 DB에서 매치를 찾아 항상 5세트 ×
(풀/하이라이트) 인라인 키보드를 보여준다. 영상 없는 슬롯도 동일하게 노출 —
BO3/BO5 구분 및 시리즈 종료 여부 자체가 스포일러가 되지 않게.

명령어:
    /watch <MMDD> <TEAM>              그 날 그 팀이 낀 매치
    /watch <MMDD> <TEAM_A> <TEAM_B>   특정 매치
    /help                             명령어 안내

날짜는 MMDD 4자리 (예: 0523). 연도는 KST 현재 연도 자동.
KST 기준 오늘보다 미래면 작년으로 해석한다 (시즌 종료 후 다시보기 케이스).

환경변수:
    TELEGRAM_BOT_TOKEN  — BotFather 토큰 (.env 파일 또는 환경변수)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

ROOT = Path(__file__).resolve().parent.parent.parent
# 배포(Docker)에서는 DB를 볼륨에 두므로 LCK_DB_PATH로 경로를 덮어쓴다.
DB_PATH = Path(os.environ["LCK_DB_PATH"]) if os.environ.get("LCK_DB_PATH") else ROOT / "data" / "vods" / "videos.sqlite"
TOTAL_SETS = 5
KST = timezone(timedelta(hours=9))


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
          chat_id     INTEGER NOT NULL,
          team        TEXT    NOT NULL,
          wins_only   INTEGER NOT NULL DEFAULT 0,
          created_at  TEXT    NOT NULL,
          PRIMARY KEY (chat_id, team)
        );
        CREATE INDEX IF NOT EXISTS idx_subscriptions_team ON subscriptions(team);

        CREATE TABLE IF NOT EXISTS notifications (
          chat_id   INTEGER NOT NULL,
          match_id  TEXT    NOT NULL,
          sent_at   TEXT    NOT NULL,
          PRIMARY KEY (chat_id, match_id)
        );

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
        """
    )
    # 마이그레이션: 기존 DB에 pre_notify(경기 시작 알림) 컬럼이 없으면 추가.
    # wins_only(=VOD 종료 후 알림 축)와 독립된 축이다.
    try:
        conn.execute(
            "ALTER TABLE subscriptions ADD COLUMN pre_notify INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # 이미 존재
    conn.commit()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("lck-bot")


def parse_mmdd(s: str) -> str | None:
    """MMDD(4자리) → 'YYYY-MM-DD'.

    KST 현재 연도로 결합. 결과가 KST 오늘보다 미래면 작년으로 해석.
    """
    if not s.isdigit() or len(s) != 4:
        return None
    mm, dd = int(s[:2]), int(s[2:])
    today = datetime.now(KST).date()
    for year in (today.year, today.year - 1):
        try:
            candidate = datetime(year, mm, dd).date()
        except ValueError:
            return None
        if candidate <= today:
            return candidate.isoformat()
    return None


def find_match_by_two(
    conn: sqlite3.Connection, date: str, team_x: str, team_y: str
) -> dict | None:
    a, b = team_x.upper(), team_y.upper()
    row = conn.execute(
        """
        SELECT match_id, league, team_a, team_b, start_at_kst, stage
          FROM matches
         WHERE match_date = ?
           AND ((team_a = ? AND team_b = ?) OR (team_a = ? AND team_b = ?))
         ORDER BY start_at_kst
         LIMIT 1
        """,
        (date, a, b, b, a),
    ).fetchone()
    if not row:
        return None
    return _row_to_match(row)


def find_matches_by_team(
    conn: sqlite3.Connection, date: str, team: str
) -> list[dict]:
    t = team.upper()
    rows = conn.execute(
        """
        SELECT match_id, league, team_a, team_b, start_at_kst, stage
          FROM matches
         WHERE match_date = ?
           AND (team_a = ? OR team_b = ?)
         ORDER BY start_at_kst
        """,
        (date, t, t),
    ).fetchall()
    return [_row_to_match(r) for r in rows]


def find_recent_matches_by_team(
    conn: sqlite3.Connection, team: str, limit: int = 10
) -> list[dict]:
    """오늘(KST)까지의 해당 팀 매치를 최신순으로."""
    t = team.upper()
    today = datetime.now(KST).date().isoformat()
    rows = conn.execute(
        """
        SELECT match_id, league, team_a, team_b, start_at_kst, stage
          FROM matches
         WHERE match_date <= ?
           AND (team_a = ? OR team_b = ?)
         ORDER BY start_at_kst DESC
         LIMIT ?
        """,
        (today, t, t, limit),
    ).fetchall()
    return [_row_to_match(r) for r in rows]


def find_upcoming_matches_by_team(
    conn: sqlite3.Connection, team: str, limit: int = 10
) -> list[dict]:
    """오늘(KST) 이후 다가오는 매치를 가까운 순으로."""
    t = team.upper()
    today = datetime.now(KST).date().isoformat()
    rows = conn.execute(
        """
        SELECT match_id, league, team_a, team_b, start_at_kst, stage
          FROM matches
         WHERE match_date >= ?
           AND (team_a = ? OR team_b = ?)
         ORDER BY start_at_kst
         LIMIT ?
        """,
        (today, t, t, limit),
    ).fetchall()
    return [_row_to_match(r) for r in rows]


def team_exists(conn: sqlite3.Connection, team: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM matches WHERE team_a = ? OR team_b = ? LIMIT 1",
        (team, team),
    ).fetchone()
    return row is not None


# VOD(종료 후) 알림 축: wins_only 컬럼 두 값으로 표현하되, 구독 자체가 없으면 "off".
#   off  = VOD 알림 안 받음 (해당 팀 구독 row에서 vod 축이 꺼진 상태)
#   all  = 모든 경기 VOD 알림 (wins_only=0)
#   wins = 이긴 경기만       (wins_only=1)
VOD_OFF, VOD_ALL, VOD_WINS = "off", "all", "wins"
VOD_CYCLE = {VOD_OFF: VOD_ALL, VOD_ALL: VOD_WINS, VOD_WINS: VOD_OFF}


def get_subscription(
    conn: sqlite3.Connection, chat_id: int, team: str
) -> tuple[str, bool]:
    """(vod, pre) 반환. 구독이 없으면 (VOD_OFF, False)."""
    row = conn.execute(
        "SELECT wins_only, pre_notify FROM subscriptions WHERE chat_id = ? AND team = ?",
        (chat_id, team),
    ).fetchone()
    if row is None:
        return VOD_OFF, False
    vod = VOD_WINS if row[0] else VOD_ALL
    return vod, bool(row[1])


def list_subscriptions(conn: sqlite3.Connection, chat_id: int) -> list[tuple[str, int, int]]:
    rows = conn.execute(
        "SELECT team, wins_only, pre_notify FROM subscriptions "
        "WHERE chat_id = ? ORDER BY team",
        (chat_id,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def set_subscription(
    conn: sqlite3.Connection, chat_id: int, team: str, vod: str, pre: bool
) -> str:
    """구독 상태를 (vod, pre) 두 축으로 명시적으로 설정.

    두 축 다 꺼짐(vod=off & pre=False)이면 구독 row를 삭제한다.
    반환: 'inserted' | 'updated' | 'removed' | 'unchanged'.
    """
    now = datetime.now(KST).isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT wins_only, pre_notify FROM subscriptions WHERE chat_id = ? AND team = ?",
        (chat_id, team),
    ).fetchone()

    if vod == VOD_OFF and not pre:
        if existing is None:
            return "unchanged"
        conn.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND team = ?",
            (chat_id, team),
        )
        conn.commit()
        return "removed"

    wo = 1 if vod == VOD_WINS else 0
    pn = 1 if pre else 0
    if existing is None:
        conn.execute(
            "INSERT INTO subscriptions (chat_id, team, wins_only, pre_notify, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, team, wo, pn, now),
        )
        conn.commit()
        return "inserted"
    if (existing[0], existing[1]) != (wo, pn):
        conn.execute(
            "UPDATE subscriptions SET wins_only = ?, pre_notify = ? "
            "WHERE chat_id = ? AND team = ?",
            (wo, pn, chat_id, team),
        )
        conn.commit()
        return "updated"
    return "unchanged"


def add_subscription(
    conn: sqlite3.Connection, chat_id: int, team: str, wins_only: bool = False
) -> str:
    """레거시 커맨드(/subscribe TEAM [wins]) 호환용.

    VOD 축만 설정하고 기존 pre_notify 상태는 보존한다.
    """
    _, pre = get_subscription(conn, chat_id, team)
    vod = VOD_WINS if wins_only else VOD_ALL
    return set_subscription(conn, chat_id, team, vod, pre)


def remove_subscription(conn: sqlite3.Connection, chat_id: int, team: str) -> bool:
    cur = conn.execute(
        "DELETE FROM subscriptions WHERE chat_id = ? AND team = ?",
        (chat_id, team),
    )
    conn.commit()
    return cur.rowcount > 0


def find_match_by_id(conn: sqlite3.Connection, match_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT match_id, league, team_a, team_b, start_at_kst, stage
          FROM matches
         WHERE match_id = ?
        """,
        (match_id,),
    ).fetchone()
    return _row_to_match(row) if row else None


def _row_to_match(row) -> dict:
    return {
        "match_id": row[0], "league": row[1],
        "team_a": row[2], "team_b": row[3],
        "start_at_kst": row[4], "stage": row[5],
    }


def find_videos(
    conn: sqlite3.Connection, date: str, team_a: str, team_b: str
) -> dict[tuple[int, str], dict]:
    rows = conn.execute(
        """
        SELECT c.set_index, c.kind, v.source, v.source_id, v.url
          FROM video_classifications c
          JOIN videos v
            ON v.source = c.source AND v.source_id = c.source_id
         WHERE c.match_date BETWEEN date(?, '-1 day') AND date(?, '+1 day')
           AND c.kind IN ('full', 'set_highlight')
           AND ((c.team_a = ? AND c.team_b = ?) OR (c.team_a = ? AND c.team_b = ?))
        """,
        (date, date, team_a, team_b, team_b, team_a),
    ).fetchall()
    out: dict[tuple[int, str], dict] = {}
    for set_index, kind, source, source_id, url in rows:
        if set_index is None:
            continue
        out[(set_index, kind)] = {
            "source": source, "source_id": source_id, "url": url,
        }
    return out


PLACEHOLDER_URL = "https://chzzk.naver.com/video/1"


def build_keyboard(
    match: dict, videos: dict[tuple[int, str], dict]
) -> InlineKeyboardMarkup:
    rows = []
    for s in range(1, TOTAL_SETS + 1):
        buttons = []
        for kind, label in (("set_highlight", "하이라이트"), ("full", "풀영상")):
            v = videos.get((s, kind))
            url = v["url"] if v else PLACEHOLDER_URL
            buttons.append(InlineKeyboardButton(label, url=url))
        rows.append(
            [InlineKeyboardButton(f"세트 {s}", callback_data=f"label:{s}")]
            + buttons
        )
    return InlineKeyboardMarkup(rows)


def build_pick_keyboard(matches: list[dict]) -> InlineKeyboardMarkup:
    """한 팀 검색 결과가 여러 개일 때 매치 선택 키보드."""
    rows = [
        [InlineKeyboardButton(
            f"[{m['league']}] {m['team_a']} vs {m['team_b']}",
            callback_data=f"show:{m['match_id']}",
        )]
        for m in matches
    ]
    return InlineKeyboardMarkup(rows)


def build_match_list_keyboard(matches: list[dict]) -> InlineKeyboardMarkup:
    """/team, /schedule용 — 날짜+매치업만, 영상 유무는 숨김."""
    rows = [
        [InlineKeyboardButton(
            f"📅 {m['start_at_kst'][5:10].replace('-', '')} {m['start_at_kst'][11:16]}  [{m['league']}] {m['team_a']} vs {m['team_b']}",
            callback_data=f"show:{m['match_id']}",
        )]
        for m in matches
    ]
    return InlineKeyboardMarkup(rows)


def match_text(match: dict) -> str:
    return (
        f"📅 {match['start_at_kst'][:10]} {match['start_at_kst'][11:16]}  [{match['league']}]\n"
        f"<b>{match['team_a']} vs {match['team_b']}</b>"
    )


async def send_match(msg_or_query, conn: sqlite3.Connection, match: dict, *, edit: bool):
    videos = find_videos(
        conn, match["start_at_kst"][:10], match["team_a"], match["team_b"]
    )
    keyboard = build_keyboard(match, videos)
    text = match_text(match)
    if edit:
        await msg_or_query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    else:
        await msg_or_query.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


HELP_TEXT = (
    "<b>LCK 무스포 다시보기 봇</b>\n\n"
    "<b>/watch</b> — 팀 선택 키보드\n"
    "<b>/watch &lt;팀&gt;</b> — 그 팀 최근 10경기\n"
    "  예: <code>/watch HLE</code>\n\n"
    "<b>/watch &lt;MMDD&gt; &lt;팀&gt;</b> — 그 날 그 팀 경기\n"
    "  예: <code>/watch 0523 DK</code>\n\n"
    "<b>/watch &lt;MMDD&gt; &lt;팀A&gt; &lt;팀B&gt;</b> — 특정 매치업\n"
    "  예: <code>/watch 0523 DK BFX</code>\n\n"
    "<b>/schedule</b> — 팀 선택 (다가오는 경기)\n"
    "<b>/schedule &lt;팀&gt;</b> — 그 팀 다가오는 10경기\n"
    "  예: <code>/schedule HLE</code>\n\n"
    "<b>/subscribe</b> — 팀 선택 (알림 등록)\n"
    "<b>/subscribe &lt;팀&gt;</b> — 버튼으로 알림 설정 (예: <code>/subscribe HLE</code>)\n"
    "  ⏰ <b>경기 시작 알림</b> — 시작 10분 전 본방 알림\n"
    "  📺 <b>종료 후 VOD 알림</b> — 전체 / 이긴 것만 / 끔\n"
    "  ⚠ '이긴 것만'은 알림이 오는 것 자체가 승리를 뜻합니다.\n\n"
    "<b>/unsubscribe</b> — 구독 중인 팀에서 해제\n"
    "<b>/subscriptions</b> — 내 구독 목록\n\n"
    "<b>/help</b> — 이 안내\n\n"
    "날짜는 4자리(MMDD). 연도는 자동.\n"
    "검색 결과는 항상 5세트 슬롯으로 보입니다."
)


PERSISTENT_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/watch"), KeyboardButton("/schedule")],
        [KeyboardButton("/subscribe"), KeyboardButton("/subscriptions")],
        [KeyboardButton("/unsubscribe"), KeyboardButton("/help")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="명령어를 누르거나 직접 입력",
)


_VOD_LABEL = {VOD_OFF: "안 받음", VOD_ALL: "전체", VOD_WINS: "이긴 것만"}


def sub_panel_text(team: str, vod: str, pre: bool) -> str:
    """토글 패널 상단 안내 텍스트 (현재 상태 요약)."""
    pre_s = "켜짐" if pre else "꺼짐"
    lines = [
        f"<b>{team}</b> 알림 설정",
        f"⏰ 경기 시작 알림: <b>{pre_s}</b> (시작 {PRE_NOTIFY_MINUTES}분 전)",
        f"📺 종료 후 VOD 알림: <b>{_VOD_LABEL[vod]}</b>",
    ]
    if vod == VOD_WINS:
        lines.append("  ⚠ 이긴 경기만 — 알림이 오는 것 자체가 승리를 뜻합니다.")
    if vod == VOD_OFF and not pre:
        lines.append("  (둘 다 꺼두면 구독이 해제됩니다.)")
    return "\n".join(lines)


def build_sub_panel_keyboard(team: str, vod: str, pre: bool) -> InlineKeyboardMarkup:
    """(시작 알림 토글, VOD 순환) 두 버튼의 인라인 패널."""
    pre_btn = f"⏰ 시작 알림: {'켜짐 ✅' if pre else '꺼짐'}"
    vod_btn = f"📺 VOD: {_VOD_LABEL[vod]}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pre_btn, callback_data=f"sub:{team}:pre")],
        [InlineKeyboardButton(vod_btn, callback_data=f"sub:{team}:vod")],
    ])


async def _render_sub_panel(msg_or_query, conn, chat_id, team, *, edit):
    vod, pre = get_subscription(conn, chat_id, team)
    text = sub_panel_text(team, vod, pre)
    kb = build_sub_panel_keyboard(team, vod, pre)
    if edit:
        await msg_or_query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=kb, disable_web_page_preview=True,
        )
    else:
        await msg_or_query.reply_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=kb, disable_web_page_preview=True,
        )


def build_team_picker_keyboard(conn: sqlite3.Connection, command: str) -> ReplyKeyboardMarkup:
    """matches에 등장한 팀들을 2열 그리드로. 누르면 '<command> <팀>' 전송."""
    teams = [
        row[0] for row in conn.execute(
            """
            SELECT DISTINCT team FROM (
              SELECT team_a AS team FROM matches
              UNION SELECT team_b FROM matches
            ) ORDER BY team
            """
        ).fetchall()
    ]
    return _teams_to_picker(teams, command)


def build_unsubscribe_picker_keyboard(
    conn: sqlite3.Connection, chat_id: int
) -> ReplyKeyboardMarkup | None:
    subs = list_subscriptions(conn, chat_id)
    if not subs:
        return None
    return _teams_to_picker([team for team, *_ in subs], "/unsubscribe")


def _teams_to_picker(teams: list[str], command: str) -> ReplyKeyboardMarkup:
    rows = []
    for i in range(0, len(teams), 2):
        chunk = teams[i:i + 2]
        rows.append([KeyboardButton(f"{command} {t}") for t in chunk])
    rows.append([KeyboardButton("/help")])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=f"팀을 누르세요 (예: {command} HLE)",
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            HELP_TEXT,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=PERSISTENT_KEYBOARD,
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            HELP_TEXT,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=PERSISTENT_KEYBOARD,
        )


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    args = ctx.args or []
    conn: sqlite3.Connection = ctx.application.bot_data["db"]

    if len(args) == 0:
        await msg.reply_text(
            "팀을 선택하세요",
            reply_markup=build_team_picker_keyboard(conn, "/watch"),
        )
        return

    if len(args) == 1 and not args[0].isdigit():
        team = args[0].upper()
        matches = find_recent_matches_by_team(conn, team, limit=10)
        if not matches:
            await msg.reply_text(
                f"{team} 팀의 최근 경기를 찾을 수 없습니다.",
                disable_web_page_preview=True,
            )
            return
        await msg.reply_text(
            f"<b>{team}</b> 최근 경기 ({len(matches)}개)\n눌러서 다시보기",
            parse_mode=ParseMode.HTML,
            reply_markup=build_match_list_keyboard(matches),
            disable_web_page_preview=True,
        )
        return

    if len(args) not in (2, 3):
        await msg.reply_text(
            "사용법:\n"
            "  /watch                       팀 선택\n"
            "  /watch <팀>                  팀 최근 10경기 (예: /watch HLE)\n"
            "  /watch <MMDD> <팀>           날짜+팀 (예: /watch 0523 DK)\n"
            "  /watch <MMDD> <팀A> <팀B>    매치업 (예: /watch 0523 DK BFX)",
            disable_web_page_preview=True,
        )
        return

    date = parse_mmdd(args[0])
    if not date:
        await msg.reply_text(
            "날짜는 MMDD 4자리로 입력하세요. 예: 0523",
            disable_web_page_preview=True,
        )
        return

    if len(args) == 3:
        match = find_match_by_two(conn, date, args[1], args[2])
        if not match:
            await msg.reply_text(
                f"해당 경기를 찾을 수 없습니다.\n{date} / {args[1].upper()} vs {args[2].upper()}",
                disable_web_page_preview=True,
            )
            return
        await send_match(msg, conn, match, edit=False)
        return

    team = args[1]
    matches = find_matches_by_team(conn, date, team)
    if not matches:
        await msg.reply_text(
            f"해당 경기를 찾을 수 없습니다.\n{date} / {team.upper()}",
            disable_web_page_preview=True,
        )
        return
    if len(matches) == 1:
        await send_match(msg, conn, matches[0], edit=False)
        return

    await msg.reply_text(
        f"📅 {date} / {team.upper()} 경기를 골라주세요",
        reply_markup=build_pick_keyboard(matches),
        disable_web_page_preview=True,
    )


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    args = ctx.args or []
    conn: sqlite3.Connection = ctx.application.bot_data["db"]

    if len(args) == 0:
        await msg.reply_text(
            "팀을 선택하세요",
            reply_markup=build_team_picker_keyboard(conn, "/schedule"),
        )
        return
    if len(args) != 1:
        await msg.reply_text(
            "사용법: /schedule <팀>   예: /schedule HLE",
            disable_web_page_preview=True,
        )
        return
    team = args[0].upper()
    matches = find_upcoming_matches_by_team(conn, team, limit=10)
    if not matches:
        await msg.reply_text(
            f"{team} 팀의 다가오는 경기가 없습니다.",
            disable_web_page_preview=True,
        )
        return
    await msg.reply_text(
        f"<b>{team}</b> 다가오는 경기 ({len(matches)}개)",
        parse_mode=ParseMode.HTML,
        reply_markup=build_match_list_keyboard(matches),
        disable_web_page_preview=True,
    )


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    args = ctx.args or []
    conn: sqlite3.Connection = ctx.application.bot_data["db"]

    if len(args) == 0:
        await msg.reply_text(
            "구독할 팀을 선택하세요",
            reply_markup=build_team_picker_keyboard(conn, "/subscribe"),
        )
        return
    if len(args) > 2:
        await msg.reply_text(
            "사용법:\n"
            "  /subscribe <팀>           예: /subscribe HLE  (버튼으로 알림 설정)\n"
            "  /subscribe <팀> wins      예: /subscribe HLE wins  (이긴 VOD만 바로 켜기)",
            disable_web_page_preview=True,
        )
        return

    team = args[0].upper()
    if not team_exists(conn, team):
        await msg.reply_text(
            f"{team} 팀이 일정에 없습니다. 팀 약어를 확인해 주세요.",
            disable_web_page_preview=True,
        )
        return

    # 레거시 호환: /subscribe TEAM wins → VOD 축을 즉시 wins로 설정.
    if len(args) == 2:
        if args[1].lower() != "wins":
            await msg.reply_text(
                "두 번째 인자는 'wins'만 가능합니다. 그냥 <code>/subscribe " + team +
                "</code> 로 버튼 설정을 쓰는 걸 권장합니다.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        _, pre = get_subscription(conn, chat.id, team)
        set_subscription(conn, chat.id, team, VOD_WINS, pre)

    # 팀이 정해지면 항상 토글 패널을 띄운다 (버튼으로 두 축을 켜고 끔).
    await _render_sub_panel(msg, conn, chat.id, team, edit=False)


async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    args = ctx.args or []
    conn: sqlite3.Connection = ctx.application.bot_data["db"]

    if len(args) == 0:
        keyboard = build_unsubscribe_picker_keyboard(conn, chat.id)
        if not keyboard:
            await msg.reply_text(
                "구독 중인 팀이 없습니다.",
                disable_web_page_preview=True,
            )
            return
        await msg.reply_text(
            "구독 해제할 팀을 선택하세요",
            reply_markup=keyboard,
        )
        return
    if len(args) != 1:
        await msg.reply_text(
            "사용법: /unsubscribe <팀>   예: /unsubscribe HLE",
            disable_web_page_preview=True,
        )
        return

    team = args[0].upper()
    removed = remove_subscription(conn, chat.id, team)
    if removed:
        await msg.reply_text(
            f"<b>{team}</b> 구독 해제했습니다.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await msg.reply_text(
            f"<b>{team}</b> 구독 중이 아닙니다.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_subscriptions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    conn: sqlite3.Connection = ctx.application.bot_data["db"]
    subs = list_subscriptions(conn, chat.id)
    if not subs:
        await msg.reply_text(
            "구독 중인 팀이 없습니다.\n/subscribe 로 등록할 수 있습니다.",
            disable_web_page_preview=True,
        )
        return
    lines = []
    for team, wo, pn in subs:
        opts = []
        if pn:
            opts.append("⏰ 시작 알림")
        opts.append("📺 VOD(이긴 것만)" if wo else "📺 VOD(전체)")
        lines.append(f"• <b>{team}</b> — " + ", ".join(opts))
    body = "\n".join(lines)
    await msg.reply_text(
        f"구독 중인 팀 ({len(subs)}개)\n{body}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# 경기 시작 알림: 시작 N분 전에 예약 발송. 앞으로 며칠치를 스캔해 예약할지.
PRE_NOTIFY_MINUTES = 10
PRE_NOTIFY_SCAN_DAYS = 3

NOTIFY_DELAY_HOURS = 4
NOTIFY_INTERVAL_SEC = 300
NOTIFY_RATE_LIMIT_SEC = 0.5


def find_due_notifications(conn: sqlite3.Connection) -> list[dict]:
    """발송 대상.

    공통 조건: start_at_kst + 4h 경과 + (chat_id, match_id) 미발송.

    wins_only=0: 매치 자체로 발송.
    wins_only=1: match_winners에 승팀 정보가 있고, 그 승팀이 구독 팀일 때만 발송.
                 인터뷰 영상 publish_date가 경기일과 ±1일 차이 가능하므로
                 match_date를 ±1일 윈도우로 매칭. 매치업 두 팀 순서 무관.
    """
    delta = f"+{NOTIFY_DELAY_HOURS} hours"
    rows = conn.execute(
        """
        SELECT s.chat_id,
               m.match_id, m.league, m.team_a, m.team_b,
               m.start_at_kst, m.stage
          FROM subscriptions s
          JOIN matches m
            ON (m.team_a = s.team OR m.team_b = s.team)
     LEFT JOIN notifications n
            ON n.chat_id = s.chat_id AND n.match_id = m.match_id
         WHERE s.wins_only = 0
           AND n.match_id IS NULL
           AND datetime(m.start_at_kst, ?) <= datetime('now', '+9 hours')

        UNION ALL

        SELECT s.chat_id,
               m.match_id, m.league, m.team_a, m.team_b,
               m.start_at_kst, m.stage
          FROM subscriptions s
          JOIN matches m
            ON (m.team_a = s.team OR m.team_b = s.team)
          JOIN match_winners w
            ON w.winner = s.team
           AND w.match_date BETWEEN date(m.match_date, '-1 day')
                                AND date(m.match_date, '+1 day')
           AND ((w.team_a = m.team_a AND w.team_b = m.team_b)
             OR (w.team_a = m.team_b AND w.team_b = m.team_a))
     LEFT JOIN notifications n
            ON n.chat_id = s.chat_id AND n.match_id = m.match_id
         WHERE s.wins_only = 1
           AND n.match_id IS NULL
           AND datetime(m.start_at_kst, ?) <= datetime('now', '+9 hours')

        ORDER BY 6, 1
        """,
        (delta, delta),
    ).fetchall()
    seen: set[tuple[int, str]] = set()
    out: list[dict] = []
    for r in rows:
        key = (r[0], r[1])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "chat_id": r[0],
            "match_id": r[1],
            "league": r[2],
            "team_a": r[3],
            "team_b": r[4],
            "start_at_kst": r[5],
            "stage": r[6],
        })
    return out


def mark_notified(conn: sqlite3.Connection, chat_id: int, match_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO notifications (chat_id, match_id, sent_at)
        VALUES (?, ?, ?)
        """,
        (chat_id, match_id, datetime.now(KST).isoformat(timespec="seconds")),
    )
    conn.commit()


def is_notified(conn: sqlite3.Connection, chat_id: int, match_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM notifications WHERE chat_id = ? AND match_id = ?",
        (chat_id, match_id),
    ).fetchone() is not None


# ----- 경기 시작 알림 (start_at_kst - PRE_NOTIFY_MINUTES 에 run_once 예약) -----

def _pre_key(match_id: str) -> str:
    """시작 알림용 notifications 키. VOD 알림(match_id)과 분리."""
    return f"{match_id}:pre"


def _parse_start(start_at_kst: str) -> datetime:
    """'YYYY-MM-DDTHH:MM:SS+09:00' → tz-aware datetime(KST)."""
    return datetime.fromisoformat(start_at_kst)


def find_pre_notify_matches(conn: sqlite3.Connection, days: int) -> list[dict]:
    """지금 ~ +days 사이 시작하는 매치 중, pre_notify=1 구독자가 하나라도 있는 것.

    매치 단위(중복 제거)로 반환. 발송 대상 chat_id 확정은 발동 시점에 다시 조회한다.
    """
    now = datetime.now(KST)
    horizon = (now + timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT DISTINCT m.match_id, m.league, m.team_a, m.team_b,
               m.start_at_kst, m.stage
          FROM matches m
          JOIN subscriptions s
            ON (m.team_a = s.team OR m.team_b = s.team)
         WHERE s.pre_notify = 1
           AND m.start_at_kst >= ?
           AND m.start_at_kst <= ?
         ORDER BY m.start_at_kst
        """,
        (now.isoformat(timespec="seconds"), horizon),
    ).fetchall()
    return [_row_to_match(r) for r in rows]


def pre_notify_recipients(conn: sqlite3.Connection, match: dict) -> list[int]:
    """이 매치의 pre 구독자 chat_id 중 아직 시작 알림을 안 받은 사람."""
    rows = conn.execute(
        """
        SELECT s.chat_id FROM subscriptions s
         WHERE s.pre_notify = 1
           AND (s.team = ? OR s.team = ?)
        """,
        (match["team_a"], match["team_b"]),
    ).fetchall()
    key = _pre_key(match["match_id"])
    seen: set[int] = set()
    out: list[int] = []
    for (chat_id,) in rows:
        if chat_id in seen or is_notified(conn, chat_id, key):
            continue
        seen.add(chat_id)
        out.append(chat_id)
    return out


def pre_notify_text(match: dict) -> str:
    """시작 알림 문구 (결과 없음 → 스포일러 없음)."""
    return (
        f"⏰ 곧 경기 시작 ({PRE_NOTIFY_MINUTES}분 후)\n"
        f"📅 {match['start_at_kst'][:10]} {match['start_at_kst'][11:16]}  [{match['league']}]\n"
        f"<b>{match['team_a']} vs {match['team_b']}</b>"
    )


async def pre_notify_worker(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """run_once 로 예약된 개별 매치 시작 알림 발송."""
    conn: sqlite3.Connection = ctx.application.bot_data["db"]
    match: dict = ctx.job.data
    recipients = pre_notify_recipients(conn, match)
    if not recipients:
        return
    log.info("시작 알림 발송 match=%s 대상 %d명", match["match_id"], len(recipients))
    text = pre_notify_text(match)
    for chat_id in recipients:
        try:
            await ctx.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning("시작 알림 실패 chat=%s match=%s err=%s",
                        chat_id, match["match_id"], e)
            continue
        mark_notified(conn, chat_id, _pre_key(match["match_id"]))
        await asyncio.sleep(NOTIFY_RATE_LIMIT_SEC)


def schedule_pre_notifications(app: "Application") -> int:
    """앞으로 PRE_NOTIFY_SCAN_DAYS 일치 매치의 시작 알림을 run_once 로 예약.

    - 이미 지난(발송 시각이 과거인) 매치는 스킵.
    - 같은 매치가 이미 예약돼 있으면(job name 중복) 스킵 → 재스캔 안전.
    반환: 이번에 새로 예약한 건수.
    """
    jq = app.job_queue
    if jq is None:
        return 0
    conn: sqlite3.Connection = app.bot_data["db"]
    now = datetime.now(KST)
    scheduled = 0
    for match in find_pre_notify_matches(conn, PRE_NOTIFY_SCAN_DAYS):
        fire_at = _parse_start(match["start_at_kst"]) - timedelta(minutes=PRE_NOTIFY_MINUTES)
        if fire_at <= now:
            continue
        job_name = f"pre:{match['match_id']}"
        if jq.get_jobs_by_name(job_name):
            continue  # 이미 예약됨
        jq.run_once(pre_notify_worker, when=fire_at, data=match, name=job_name)
        scheduled += 1
    if scheduled:
        log.info("시작 알림 %d건 예약 (앞으로 %d일)", scheduled, PRE_NOTIFY_SCAN_DAYS)
    return scheduled


async def pre_notify_rescan(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """하루 1회 재스캔 잡 — 신규 구독/일정 반영 & 재시작 복구."""
    schedule_pre_notifications(ctx.application)


async def notify_worker(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn: sqlite3.Connection = ctx.application.bot_data["db"]
    due = find_due_notifications(conn)
    if not due:
        return
    log.info("알림 발송 대상 %d건", len(due))
    for item in due:
        match = {
            "match_id": item["match_id"],
            "league": item["league"],
            "team_a": item["team_a"],
            "team_b": item["team_b"],
            "start_at_kst": item["start_at_kst"],
            "stage": item["stage"],
        }
        videos = find_videos(
            conn, match["start_at_kst"][:10], match["team_a"], match["team_b"]
        )
        try:
            await ctx.bot.send_message(
                chat_id=item["chat_id"],
                text=match_text(match),
                parse_mode=ParseMode.HTML,
                reply_markup=build_keyboard(match, videos),
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning(
                "알림 발송 실패 chat=%s match=%s err=%s",
                item["chat_id"], item["match_id"], e,
            )
            continue
        mark_notified(conn, item["chat_id"], item["match_id"])
        await asyncio.sleep(NOTIFY_RATE_LIMIT_SEC)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    conn: sqlite3.Connection = ctx.application.bot_data["db"]

    if data.startswith("label:"):
        await q.answer()
        return
    if data.startswith("sub:"):
        # sub:<team>:<axis>  — axis in {pre, vod}. 해당 축을 토글/순환 후 패널 갱신.
        parts = data.split(":")
        if len(parts) != 3:
            await q.answer()
            return
        _, team, axis = parts
        chat_id = q.message.chat.id if q.message else None
        if chat_id is None:
            await q.answer()
            return
        vod, pre = get_subscription(conn, chat_id, team)
        if axis == "pre":
            pre = not pre
        elif axis == "vod":
            vod = VOD_CYCLE[vod]
        else:
            await q.answer()
            return
        status = set_subscription(conn, chat_id, team, vod, pre)
        # 시작 알림을 새로 켰으면 즉시 예약 반영.
        if pre and axis == "pre":
            schedule_pre_notifications(ctx.application)
        if status == "removed":
            await q.answer("구독 해제됨")
            await q.edit_message_text(
                f"<b>{team}</b> 구독을 해제했습니다.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        await q.answer("변경됨")
        await _render_sub_panel(q, conn, chat_id, team, edit=True)
        return
    if data.startswith("show:"):
        match_id = data.split(":", 1)[1]
        match = find_match_by_id(conn, match_id)
        if not match:
            await q.answer("매치를 찾을 수 없습니다.", show_alert=True)
            return
        await q.answer()
        await send_match(q, conn, match, edit=True)
        return
    await q.answer()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


BOT_COMMANDS = [
    BotCommand("watch", "다시보기 — 팀/날짜로 매치 찾기"),
    BotCommand("schedule", "다가오는 경기 일정"),
    BotCommand("subscribe", "팀 알림 구독"),
    BotCommand("unsubscribe", "구독 해제"),
    BotCommand("subscriptions", "내 구독 목록"),
    BotCommand("help", "명령어 안내"),
    BotCommand("start", "시작"),
]


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)


def main() -> int:
    load_dotenv(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN이 필요합니다. .env 파일 또는 환경변수로 설정하세요.",
            file=sys.stderr,
        )
        return 1
    if not DB_PATH.exists():
        print(f"DB 없음: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_schema(conn)

    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["db"] = conn
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("subscriptions", cmd_subscriptions))
    app.add_handler(CallbackQueryHandler(on_callback))

    if app.job_queue is None:
        log.warning(
            "JobQueue 비활성 — 'pip install python-telegram-bot[job-queue]' 필요. "
            "알림 워커를 건너뜁니다."
        )
    else:
        app.job_queue.run_repeating(
            notify_worker, interval=NOTIFY_INTERVAL_SEC, first=5
        )
        log.info(
            "알림 워커 등록: %ds 주기, start_at_kst+%dh 경과 매치 발송",
            NOTIFY_INTERVAL_SEC, NOTIFY_DELAY_HOURS,
        )
        # 경기 시작 알림: 재스캔 잡이 봇 시작 직후(first=10s) 최초 예약을 하고,
        # 이후 24h마다 재스캔해 신규 구독/일정 반영 & 재시작 복구.
        app.job_queue.run_repeating(
            pre_notify_rescan, interval=timedelta(days=1), first=10
        )
        log.info(
            "시작 알림 재스캔 등록: 24h 주기, 시작 %d분 전 예약 (앞으로 %d일)",
            PRE_NOTIFY_MINUTES, PRE_NOTIFY_SCAN_DAYS,
        )

    log.info("봇 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
