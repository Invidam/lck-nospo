#!/usr/bin/env bash
# ============================================================================
# 컨테이너 진입점 — lck-nospo (stock-routine 패턴 이식)
#
# 두 상시/배치 컴포넌트를 MODE 로 분기한다:
#   MODE=bot   (기본) : (부트스트랩) → 텔레그램 봇 상시 polling
#   MODE=fetch        : VOD 수집 파이프라인 1회 실행 후 종료 (cron 배치용)
#   MODE=schedule     : 구독형 Worlds 일정 동기화 1회 실행 후 종료
#
# 데이터는 볼륨(/data)의 단일 SQLite(videos.sqlite)에 모인다.
# 봇과 fetch 컨테이너가 같은 볼륨을 공유한다 (stock-routine 의 portfolio.db 와 동형).
#
# 부트스트랩(봇 최초 기동 시 DB가 비어 있으면):
#   1) 이미지에 동봉된 data/schedules/*.ics 를 matches 로 import
#   2) VOD 수집 파이프라인 1회 실행 (fetch → classify → winners)
#   → 이후 봇이 뜬다. DB가 이미 있으면 이 단계는 건너뛴다.
# ============================================================================
set -euo pipefail

export PYTHONPATH="/app:${PYTHONPATH:-}"
export LCK_DB_PATH="${LCK_DB_PATH:-/data/videos.sqlite}"
MODE="${MODE:-bot}"
PY="python"

mkdir -p "$(dirname "$LCK_DB_PATH")"

echo "=== lck-nospo 진입점 (MODE=$MODE) ==="
echo "  DB : $LCK_DB_PATH"

run_pipeline() {
  "$PY" schedule/scripts/sync_ical_schedule.py \
    --season "${LCK_SCHEDULE_SEASON:-2026}" \
    --from-date "${LCK_SCHEDULE_FROM_DATE:-2026-10-24}" \
    --apply || echo "  일정 동기화 실패: schedule_sync_runs 확인 (계속)"
  # 두 치지직 채널 fetch → classify(신규) → winners(신규).
  # 한 채널 실패해도 다음 채널 시도 (부분 장애 내성).
  echo "--- [fetch] 치지직 VOD 수집 파이프라인 ---"
  for ch in "${CHZZK_CHANNELS[@]}"; do
    "$PY" vod_index/scripts/fetch_chzzk_vods.py "$ch" || echo "  fetch 실패: $ch (계속)"
  done
  "$PY" vod_index/scripts/classify_videos.py --only-new || echo "  classify 실패 (계속)"
  "$PY" vod_index/scripts/extract_winners.py --only-new || echo "  winners 실패 (계속)"
}

# 채널 ID (README 표 참조): LCK 공식 / EWC 공식(RTE)
CHZZK_CHANNELS=(
  "${CHZZK_CHANNEL_LCK:-9381e7d6816e6d915a44a13c0195b202}"
  "${CHZZK_CHANNEL_EWC:-fce7c8735e0646e642007198a8875882}"
)

# ---- MODE=fetch : 수집만 하고 종료 (cron 배치) ----------------------------
if [ "$MODE" = "fetch" ]; then
  run_pipeline
  echo "=== MODE=fetch : 수집 완료, 종료 ==="
  exit 0
fi

# ---- MODE=schedule : Worlds 일정 동기화 후 종료 ---------------------------
if [ "$MODE" = "schedule" ]; then
  "$PY" schedule/scripts/sync_ical_schedule.py \
    --season "${LCK_SCHEDULE_SEASON:-2026}" \
    --from-date "${LCK_SCHEDULE_FROM_DATE:-2026-10-24}" \
    --apply
  echo "=== MODE=schedule : 일정 동기화 완료, 종료 ==="
  exit 0
fi

# ---- MODE=bot : 부트스트랩 후 봇 상시 실행 --------------------------------
if [ ! -f "$LCK_DB_PATH" ]; then
  echo "--- [bootstrap] DB 없음 → 일정 import + 초기 수집 ---"
  # 이미지 동봉 일정(.ics) → matches. 없으면 조용히 넘어간다.
  shopt -s nullglob
  ICS=(data/schedules/*.ics)
  shopt -u nullglob
  if [ ${#ICS[@]} -gt 0 ]; then
    "$PY" schedule/scripts/import_schedule.py "${ICS[@]}" || echo "  일정 import 실패 (계속)"
  else
    echo "  data/schedules/*.ics 없음 — 일정 import 건너뜀"
  fi
  run_pipeline
fi

echo "--- [bot] 텔레그램 봇 polling 시작 ---"
exec "$PY" notifier/scripts/telegram_bot.py
