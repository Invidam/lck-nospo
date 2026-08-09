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

    telegram = types.ModuleType("telegram")
    telegram.BotCommand = _Stub
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    telegram.KeyboardButton = _Stub
    telegram.ReplyKeyboardMarkup = _Stub
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
