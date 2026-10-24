"""match_text() / build_match_list_keyboard() 출력에 시작 시간(HH:MM)이 포함되는지 검증.

telegram 패키지 없이도 실제 telegram_bot 모듈을 import하기 위해 최소 stub을
sys.modules에 주입한다. 실제 함수를 그대로 호출하므로 코드 경로가 검증된다.

실행: python notifier/tests/test_match_display.py   (표준 unittest, 외부 의존성 없음)
"""
from __future__ import annotations

import sqlite3
import sys
import types
import unittest
from unittest.mock import Mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "notifier" / "scripts"


def _install_telegram_stub() -> None:
    """telegram_bot이 import하는 심볼만 갖춘 가짜 telegram 패키지를 주입."""
    if "telegram" in sys.modules:
        return

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None, **kw):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class _Stub:
        def __init__(self, *a, **k):
            pass

    class KeyboardButton:
        def __init__(self, text, **kw):
            self.text = text

    class ReplyKeyboardMarkup:
        def __init__(self, keyboard, **kw):
            self.keyboard = keyboard

    telegram = types.ModuleType("telegram")
    telegram.BotCommand = _Stub
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    telegram.KeyboardButton = KeyboardButton
    telegram.ReplyKeyboardMarkup = ReplyKeyboardMarkup
    telegram.Update = _Stub

    constants = types.ModuleType("telegram.constants")
    constants.ParseMode = types.SimpleNamespace(HTML="HTML")

    ext = types.ModuleType("telegram.ext")
    ext.Application = _Stub
    ext.CallbackQueryHandler = _Stub
    ext.CommandHandler = _Stub
    ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)

    sys.modules["telegram"] = telegram
    sys.modules["telegram.constants"] = constants
    sys.modules["telegram.ext"] = ext


_install_telegram_stub()
sys.path.insert(0, str(SCRIPTS))
import telegram_bot as tb  # noqa: E402


def _match(start_at_kst: str) -> dict:
    return {
        "match_id": "m1",
        "league": "LCK",
        "team_a": "HLE",
        "team_b": "DK",
        "start_at_kst": start_at_kst,
        "stage": "Rounds 3-4",
    }


class MatchTextTime(unittest.TestCase):
    def test_detail_text_includes_time(self):
        m = _match("2026-07-30T19:00:00+09:00")
        text = tb.match_text(m)
        self.assertIn("2026-07-30 19:00", text)
        self.assertIn("HLE vs DK", text)

    def test_list_button_includes_time(self):
        m = _match("2026-08-05T17:00:00+09:00")
        kb = tb.build_match_list_keyboard([m])
        label = kb.inline_keyboard[0][0].text
        # MMDD + 공백 + HH:MM
        self.assertIn("0805 17:00", label)
        self.assertIn("HLE vs DK", label)

    def test_afternoon_and_evening_slots(self):
        for start, hhmm in [
            ("2026-04-01T17:00:00+09:00", "17:00"),
            ("2026-04-01T19:00:00+09:00", "19:00"),
            ("2026-05-04T16:00:00+09:00", "16:00"),  # EWC 예선 16시 슬롯
        ]:
            with self.subTest(start=start):
                self.assertIn(hhmm, tb.match_text(_match(start)))
                self.assertIn(hhmm, tb.build_match_list_keyboard([_match(start)]).inline_keyboard[0][0].text)


class RealDbSmoke(unittest.TestCase):
    """실제 DB가 있으면, 실제 조회 함수 결과에도 시간이 붙는지 확인."""

    def setUp(self):
        db = ROOT / "data" / "vods" / "videos.sqlite"
        if not db.exists():
            self.skipTest("videos.sqlite 없음")
        self.conn = sqlite3.connect(db)
        n = self.conn.execute("SELECT count(*) FROM matches").fetchone()[0]
        if n == 0:
            self.skipTest("matches 테이블 비어있음")

    def test_recent_matches_render_with_time(self):
        # 아무 팀이나 하나 골라 최근 경기 조회
        team = self.conn.execute("SELECT team_a FROM matches LIMIT 1").fetchone()[0]
        matches = tb.find_recent_matches_by_team(self.conn, team, limit=3)
        self.assertTrue(matches, f"{team} 최근 경기가 있어야 함")
        for m in matches:
            text = tb.match_text(m)
            # 'YYYY-MM-DD HH:MM' 형태로 시간(콜론)이 날짜 뒤에 존재
            self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
            label = tb.build_match_list_keyboard([m]).inline_keyboard[0][0].text
            self.assertRegex(label, r"\d{4} \d{2}:\d{2}")


def _mem_db_with_matches(rows):
    """인메모리 DB + init_schema + matches 최소 스키마 + 주어진 매치 적재."""
    conn = sqlite3.connect(":memory:")
    tb.init_schema(conn)
    conn.execute(
        """
        CREATE TABLE matches (
          match_id TEXT PRIMARY KEY, league TEXT, season INTEGER, stage TEXT,
          team_a TEXT, team_b TEXT, start_at_kst TEXT, match_date TEXT,
          source_file TEXT, imported_at TEXT
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO matches (match_id, league, team_a, team_b, start_at_kst, "
            "match_date, stage) VALUES (?,?,?,?,?,?,?)",
            (r["match_id"], r.get("league", "LCK"), r["team_a"], r["team_b"],
             r["start_at_kst"], r["start_at_kst"][:10], r.get("stage", "")),
        )
    conn.commit()
    return conn


class SubscriptionToggle(unittest.TestCase):
    def setUp(self):
        self.conn = _mem_db_with_matches([])

    def test_vod_cycle_order(self):
        self.assertEqual(tb.VOD_CYCLE[tb.VOD_OFF], tb.VOD_ALL)
        self.assertEqual(tb.VOD_CYCLE[tb.VOD_ALL], tb.VOD_WINS)
        self.assertEqual(tb.VOD_CYCLE[tb.VOD_WINS], tb.VOD_OFF)

    def test_default_is_off(self):
        self.assertEqual(tb.get_subscription(self.conn, 1, "HLE"), (tb.VOD_OFF, False))

    def test_set_and_roundtrip(self):
        tb.set_subscription(self.conn, 1, "HLE", tb.VOD_ALL, True)
        self.assertEqual(tb.get_subscription(self.conn, 1, "HLE"), (tb.VOD_ALL, True))
        tb.set_subscription(self.conn, 1, "HLE", tb.VOD_WINS, False)
        self.assertEqual(tb.get_subscription(self.conn, 1, "HLE"), (tb.VOD_WINS, False))

    def test_both_off_removes_row(self):
        tb.set_subscription(self.conn, 1, "HLE", tb.VOD_ALL, True)
        status = tb.set_subscription(self.conn, 1, "HLE", tb.VOD_OFF, False)
        self.assertEqual(status, "removed")
        self.assertEqual(tb.list_subscriptions(self.conn, 1), [])

    def test_legacy_add_subscription_preserves_pre(self):
        # 시작 알림만 켠 상태에서 레거시 add_subscription(wins) 호출 → pre 보존
        tb.set_subscription(self.conn, 1, "HLE", tb.VOD_OFF, True)
        tb.add_subscription(self.conn, 1, "HLE", wins_only=True)
        self.assertEqual(tb.get_subscription(self.conn, 1, "HLE"), (tb.VOD_WINS, True))

    def test_migration_idempotent(self):
        # init_schema를 두 번 돌려도 pre_notify ALTER가 터지지 않아야 함
        tb.init_schema(self.conn)
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(subscriptions)")]
        self.assertIn("pre_notify", cols)


class TeamPickers(unittest.TestCase):
    def test_all_team_pickers_show_exactly_lck_ten(self):
        conn = _mem_db_with_matches([
            {"match_id": "worlds", "team_a": "G2", "team_b": "KC",
             "start_at_kst": "2026-10-25T02:00:00+09:00", "league": "WORLDS"},
        ])
        for command in ("/watch", "/schedule", "/subscribe"):
            keyboard = tb.build_team_picker_keyboard(conn, command)
            labels = [button.text for row in keyboard.keyboard for button in row]
            self.assertEqual(labels, [f"{command} {team}" for team in tb.LCK_TEAMS] + ["/help"])
            self.assertEqual(len(tb.LCK_TEAMS), 10)
        conn.close()

    def test_unsubscribe_picker_only_shows_subscribed_lck_teams(self):
        conn = _mem_db_with_matches([])
        tb.set_subscription(conn, 42, "G2", tb.VOD_ALL, False)
        self.assertIsNone(tb.build_unsubscribe_picker_keyboard(conn, 42))
        tb.set_subscription(conn, 42, "HLE", tb.VOD_ALL, False)
        keyboard = tb.build_unsubscribe_picker_keyboard(conn, 42)
        labels = [button.text for row in keyboard.keyboard for button in row]
        self.assertEqual(labels, ["/unsubscribe HLE", "/help"])
        self.assertEqual(len(tb.list_subscriptions(conn, 42)), 2)
        conn.close()


class PreNotify(unittest.TestCase):
    def test_schedule_changes_replace_existing_job(self):
        match = {"match_id": "m1", "team_a": "HLE", "team_b": "GEN",
                 "start_at_kst": self._future(1)}
        conn = _mem_db_with_matches([match])
        tb.set_subscription(conn, 42, "HLE", tb.VOD_OFF, True)
        app = Mock()
        app.bot_data = {"db": conn}
        old_job = Mock()
        old_job.data = {**match, "start_at_kst": self._future(1, "18:00")}
        app.job_queue.get_jobs_by_name.return_value = (old_job,)
        self.assertEqual(tb.schedule_pre_notifications(app), 1)
        old_job.schedule_removal.assert_called_once()
        self.assertEqual(app.job_queue.run_once.call_args.kwargs["data"]["start_at_kst"], match["start_at_kst"])
        conn.close()

    def test_unchanged_schedule_keeps_existing_job(self):
        match = {"match_id": "m1", "team_a": "HLE", "team_b": "GEN",
                 "start_at_kst": self._future(1)}
        conn = _mem_db_with_matches([match])
        tb.set_subscription(conn, 42, "HLE", tb.VOD_OFF, True)
        app = Mock()
        app.bot_data = {"db": conn}
        old_job = Mock()
        old_job.data = tb.find_pre_notify_matches(conn, 3)[0]
        app.job_queue.get_jobs_by_name.return_value = (old_job,)
        self.assertEqual(tb.schedule_pre_notifications(app), 0)
        old_job.schedule_removal.assert_not_called()
        app.job_queue.run_once.assert_not_called()
        conn.close()

    def _future(self, days, hhmm="19:00"):
        from datetime import datetime, timedelta
        d = (datetime.now(tb.KST) + timedelta(days=days)).date().isoformat()
        return f"{d}T{hhmm}:00+09:00"

    def test_scan_only_includes_subscribed_and_in_window(self):
        rows = [
            {"match_id": "m-hle", "team_a": "HLE", "team_b": "GEN",
             "start_at_kst": self._future(1)},
            {"match_id": "m-far", "team_a": "HLE", "team_b": "T1",
             "start_at_kst": self._future(10)},   # 3일 밖
            {"match_id": "m-other", "team_a": "DK", "team_b": "KT",
             "start_at_kst": self._future(1)},     # 구독 없음
        ]
        conn = _mem_db_with_matches(rows)
        tb.set_subscription(conn, 1, "HLE", tb.VOD_OFF, True)  # 시작 알림만
        found = {m["match_id"] for m in tb.find_pre_notify_matches(conn, 3)}
        self.assertEqual(found, {"m-hle"})

    def test_recipients_excludes_already_notified(self):
        rows = [{"match_id": "m1", "team_a": "HLE", "team_b": "GEN",
                 "start_at_kst": self._future(1)}]
        conn = _mem_db_with_matches(rows)
        tb.set_subscription(conn, 42, "HLE", tb.VOD_OFF, True)
        match = tb.find_pre_notify_matches(conn, 3)[0]
        self.assertEqual(tb.pre_notify_recipients(conn, match), [42])
        tb.mark_notified(conn, 42, tb._pre_key("m1"))
        self.assertEqual(tb.pre_notify_recipients(conn, match), [])

    def test_pre_text_has_time_and_no_result(self):
        match = {"match_id": "m1", "league": "LCK", "team_a": "HLE",
                 "team_b": "GEN", "start_at_kst": "2026-08-13T19:00:00+09:00",
                 "stage": "Rounds 3-4"}
        text = tb.pre_notify_text(match)
        self.assertIn("19:00", text)
        self.assertIn("HLE vs GEN", text)
        self.assertIn("곧 경기 시작", text)
        # 결과/승패 단어가 없어야 함(스포일러 방지)
        for spoiler in ("승", "패", "win", "이겼", "졌"):
            self.assertNotIn(spoiler, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
