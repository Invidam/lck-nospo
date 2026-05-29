#!/usr/bin/env python3
"""LCK 일정 CSV에서 특정 팀의 경기만 추출해 data/schedules/<team>/에 저장한다.

사용법:
    python schedule/scripts/extract_team_schedule.py <TEAM> [--source <CSV> ...]

예:
    # data/schedules/*.ics 전체를 source로 사용 (토너먼트별로 출력 파일이 분리됨)
    python schedule/scripts/extract_team_schedule.py HLE

    # 명시적으로 소스 지정
    python schedule/scripts/extract_team_schedule.py T1 --source data/schedules/rounds-1-2.ics
    python schedule/scripts/extract_team_schedule.py HLE \
        --source data/schedules/rounds-1-2.ics data/schedules/rte.ics

출력 파일명은 source 파일명을 그대로 따라간다 (`<team>/<team>-<source_stem>.ics`).
같은 디렉터리에 새 토너먼트 .ics를 추가해도 호출 명령은 그대로 두면 자동으로 잡힌다.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEDULES_DIR = ROOT / "data" / "schedules"

MATCHUP_RE = re.compile(r" - (?P<a>[A-Z0-9]+) vs (?P<b>[A-Z0-9]+)$")


def extract(team: str, source: Path) -> list[dict[str, str]]:
    team = team.upper()
    with source.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            m = MATCHUP_RE.search(row["Subject"])
            if not m:
                continue
            a, b = m.group("a"), m.group("b")
            if team in (a, b):
                rows.append(row)
    return rows


def write(team: str, source: Path, rows: list[dict[str, str]]) -> Path:
    team_dir = SCHEDULES_DIR / team.lower()
    team_dir.mkdir(parents=True, exist_ok=True)
    out = team_dir / f"{team.lower()}-{source.stem}.ics"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Subject", "Start Date", "Start Time"])
        writer.writeheader()
        writer.writerows(rows)
    return out


def resolve_sources(sources: list[Path]) -> list[Path]:
    if sources:
        return sources
    return sorted(p for p in SCHEDULES_DIR.glob("*.ics") if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract one team's LCK schedule.")
    parser.add_argument("team", help="팀 약어 (예: HLE, T1, GEN)")
    parser.add_argument(
        "--source",
        type=Path,
        nargs="*",
        default=[],
        help="소스 .ics 파일들. 생략 시 data/schedules/*.ics 전체 사용.",
    )
    args = parser.parse_args()

    sources = resolve_sources(args.source)
    if not sources:
        print(f"no source files found in {SCHEDULES_DIR}", file=sys.stderr)
        return 1

    total_matches = 0
    total_files = 0
    for source in sources:
        if not source.exists():
            print(f"source not found: {source}", file=sys.stderr)
            return 1
        rows = extract(args.team, source)
        if not rows:
            print(f"{source.name}: no matches for team {args.team}", file=sys.stderr)
            continue
        out = write(args.team, source, rows)
        print(f"wrote {len(rows)} matches to {out.relative_to(ROOT)}")
        total_matches += len(rows)
        total_files += 1

    if total_files == 0:
        return 2
    print(f"-- {total_files} file(s), {total_matches} match(es) total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
