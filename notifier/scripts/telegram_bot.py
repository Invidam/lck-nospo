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
DB_PATH = ROOT / "data" / "vods" / "videos.sqlite"
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


def list_subscriptions(conn: sqlite3.Connection, chat_id: int) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT team, wins_only FROM subscriptions WHERE chat_id = ? ORDER BY team",
        (chat_id,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def add_subscription(
    conn: sqlite3.Connection, chat_id: int, team: str, wins_only: bool = False
) -> str:
    """반환: 'inserted' | 'updated' | 'unchanged'.

    같은 팀이 이미 구독 중인데 wins_only 옵션이 다르면 UPDATE (덮어쓰기).
    """
    now = datetime.now(KST).isoformat(timespec="seconds")
    wo = 1 if wins_only else 0
    existing = conn.execute(
        "SELECT wins_only FROM subscriptions WHERE chat_id = ? AND team = ?",
        (chat_id, team),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO subscriptions (chat_id, team, wins_only, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, team, wo, now),
        )
        conn.commit()
        return "inserted"
    if existing[0] != wo:
        conn.execute(
            "UPDATE subscriptions SET wins_only = ? WHERE chat_id = ? AND team = ?",
            (wo, chat_id, team),
        )
        conn.commit()
        return "updated"
    return "unchanged"


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
            f"📅 {m['start_at_kst'][5:10].replace('-', '')}  [{m['league']}] {m['team_a']} vs {m['team_b']}",
            callback_data=f"show:{m['match_id']}",
        )]
        for m in matches
    ]
    return InlineKeyboardMarkup(rows)


def match_text(match: dict) -> str:
    return (
        f"📅 {match['start_at_kst'][:10]}  [{match['league']}]\n"
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
    "<b>/subscribe &lt;팀&gt;</b> — 승패 모두 알림 (예: <code>/subscribe HLE</code>)\n"
    "<b>/subscribe &lt;팀&gt; wins</b> — 이겼을 때만 알림\n"
    "  ⚠ 알림이 오는 것 자체가 승리를 알려줍니다.\n\n"
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
    return _teams_to_picker([team for team, _ in subs], "/unsubscribe")


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
            "  /subscribe <팀>           예: /subscribe HLE  (승패 모두 알림)\n"
            "  /subscribe <팀> wins      예: /subscribe HLE wins  (이겼을 때만)",
            disable_web_page_preview=True,
        )
        return

    team = args[0].upper()
    wins_only = False
    if len(args) == 2:
        if args[1].lower() != "wins":
            await msg.reply_text(
                "두 번째 인자는 'wins'만 가능합니다.\n"
                "  /subscribe <팀>           예: /subscribe HLE\n"
                "  /subscribe <팀> wins      예: /subscribe HLE wins",
                disable_web_page_preview=True,
            )
            return
        wins_only = True

    if not team_exists(conn, team):
        await msg.reply_text(
            f"{team} 팀이 일정에 없습니다. 팀 약어를 확인해 주세요.",
            disable_web_page_preview=True,
        )
        return

    status = add_subscription(conn, chat.id, team, wins_only=wins_only)
    if wins_only:
        body = (
            f"<b>{team}</b> 구독 완료 (이겼을 때만).\n\n"
            "⚠ 이 옵션은 <b>알림이 오는 것 자체가 승리를 알려줍니다.</b> "
            "졌을 땐 알림이 영영 오지 않습니다 (인터뷰 영상 미공개 시도 마찬가지).\n"
            "승패 모두 받고 싶으면 <code>/subscribe " + team + "</code> 로 덮어쓰세요."
        )
    else:
        body = f"<b>{team}</b> 구독 완료. 경기 종료 추정 시점에 알림을 보냅니다."

    if status == "inserted":
        await msg.reply_text(body, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    elif status == "updated":
        await msg.reply_text(
            f"<b>{team}</b> 옵션 변경됨.\n\n" + body,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        suffix = " (이겼을 때만)" if wins_only else ""
        await msg.reply_text(
            f"<b>{team}</b>{suffix} 이미 구독 중입니다.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


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
    for team, wo in subs:
        suffix = " <i>(이겼을 때만)</i>" if wo else ""
        lines.append(f"• <b>{team}</b>{suffix}")
    body = "\n".join(lines)
    await msg.reply_text(
        f"구독 중인 팀 ({len(subs)}개)\n{body}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


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

    log.info("봇 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
