#!/bin/bash
set -euo pipefail

REPO="/Users/invidam/IdeaProjects/lck-nospo"
PY="$REPO/.venv/bin/python"
LOG="/tmp/chzzk-fetch.log"

# 치지직 채널 ID
#   LCK 공식: 정규 시즌 (Rounds 1-2 등)
#   EWC 공식 채널A: Road to EWC / RTE 토너먼트
CHANNELS=(
  "9381e7d6816e6d915a44a13c0195b202"
  "fce7c8735e0646e642007198a8875882"
)

cd "$REPO"

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') fetch pipeline start ==="
  "$PY" schedule/scripts/sync_ical_schedule.py \
    --season "${LCK_SCHEDULE_SEASON:-2026}" \
    --from-date "${LCK_SCHEDULE_FROM_DATE:-2026-10-24}" --apply \
    || echo "일정 동기화 실패: schedule_sync_runs 확인 (계속)"
  for ch in "${CHANNELS[@]}"; do
    "$PY" vod_index/scripts/fetch_chzzk_vods.py "$ch" || echo "fetch failed: $ch"
  done
  "$PY" vod_index/scripts/classify_videos.py --only-new \
    && "$PY" vod_index/scripts/extract_winners.py --only-new
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') fetch pipeline end ==="
} >> "$LOG" 2>&1
