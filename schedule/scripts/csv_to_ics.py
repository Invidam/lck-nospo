#!/usr/bin/env python3
"""LCK 일정 CSV(.ics 확장자지만 실제 CSV)를 진짜 iCalendar(.ics)로 변환한다.

각 경기에 VALARM(기본 1일 전)을 박아 넣어, Google Calendar import 시
캘린더 기본 알림 설정과 무관하게 항상 알림이 울리게 한다.

사용법:
    python schedule/scripts/csv_to_ics.py <SOURCE_CSV> [--out <OUT.ics>] [--alarm <PT|-P*>]
                                              [--duration <ISO8601>]

예:
    python schedule/scripts/csv_to_ics.py data/schedules/hle/hle-rounds-1-2.ics
    python schedule/scripts/csv_to_ics.py data/schedules/rounds-1-2.ics --alarm -PT1H
    python schedule/scripts/csv_to_ics.py data/schedules/hle/hle-rounds-1-2.ics \\
        --out data/schedules/hle/hle-rounds-1-2-with-alarm.ics --alarm -P1D

알람 표기는 iCalendar TRIGGER 값(`-P1D`=1일 전, `-PT1H`=1시간 전, `-PT30M`=30분 전).
KST 기준 DTSTART/DTEND를 TZID=Asia/Seoul로 출력한다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

MATCHUP_RE = re.compile(r" - (?P<a>[A-Z0-9]+) vs (?P<b>[A-Z0-9]+)$")
DEFAULT_DURATION_MIN = 180

VTIMEZONE_KST = """BEGIN:VTIMEZONE
TZID:Asia/Seoul
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0900
TZOFFSETTO:+0900
TZNAME:KST
END:STANDARD
END:VTIMEZONE"""


def fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def fmt_utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def make_uid(subject: str, start: datetime) -> str:
    h = hashlib.sha1(f"{subject}|{start.isoformat()}".encode()).hexdigest()[:16]
    return f"{h}@lck-helper"


def build_vevent(subject: str, start: datetime, duration_min: int, alarm: str | None) -> str:
    end = start + timedelta(minutes=duration_min)
    dtstamp = fmt_utc(datetime.now(timezone.utc).replace(tzinfo=None))
    lines = [
        "BEGIN:VEVENT",
        f"UID:{make_uid(subject, start)}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID=Asia/Seoul:{fmt_local(start)}",
        f"DTEND;TZID=Asia/Seoul:{fmt_local(end)}",
        f"SUMMARY:{subject}",
    ]
    if alarm:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{subject}",
            f"TRIGGER:{alarm}",
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def build_calendar(events: list[str]) -> str:
    head = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//lck-helper//csv-to-ics//KO",
        "CALSCALE:GREGORIAN",
        VTIMEZONE_KST,
    ]
    tail = ["END:VCALENDAR"]
    return "\r\n".join(head + events + tail) + "\r\n"


def convert(source: Path, duration_min: int, alarm: str | None) -> str:
    events: list[str] = []
    with source.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            subject = row["Subject"]
            if not MATCHUP_RE.search(subject):
                continue
            start = datetime.strptime(
                f"{row['Start Date']} {row['Start Time']}", "%Y-%m-%d %H:%M"
            )
            events.append(build_vevent(subject, start, duration_min, alarm))
    return build_calendar(events)


def default_out(source: Path) -> Path:
    stem = source.stem
    return source.with_name(f"{stem}-cal.ics")


def main() -> int:
    parser = argparse.ArgumentParser(description="CSV 일정을 VALARM 포함 iCalendar로 변환")
    parser.add_argument("source", type=Path, help="입력 CSV (확장자 .ics이어도 됨)")
    parser.add_argument("--out", type=Path, default=None, help="출력 .ics 경로")
    parser.add_argument(
        "--alarm",
        default="-P1D",
        help="iCalendar TRIGGER (-P1D=1일 전, -PT1H=1시간 전, 'none'=알람 없음)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION_MIN,
        help="경기 길이(분), 기본 180",
    )
    args = parser.parse_args()

    if not args.source.exists():
        print(f"source not found: {args.source}", file=sys.stderr)
        return 1

    alarm = None if args.alarm.lower() == "none" else args.alarm
    out_path = args.out or default_out(args.source)

    ical = convert(args.source, args.duration, alarm)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(ical, encoding="utf-8")

    rel = out_path.relative_to(ROOT) if out_path.is_absolute() else out_path
    alarm_desc = alarm or "없음"
    print(f"wrote iCalendar to {rel} (알람: {alarm_desc}, 길이: {args.duration}분)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
