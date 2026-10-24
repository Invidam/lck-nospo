#!/usr/bin/env bash
# ============================================================================
# deploy.sh — 로컬 소스를 lck-nospo LXC(Docker)로 배포한다.
#             (stock-routine/deploy.sh 패턴 이식)
#
# 흐름:
#   1) 로컬 .env(토큰) → LXC /opt/lck-nospo/.env        (런타임 주입용, 이미지엔 안 굽는다)
#   2) 로컬 소스        → 호스트 /tmp → LXC /root/lck-nospo
#   3) LXC 안에서 docker build (이미지 lck-nospo)
#   4) bot 컨테이너 재기동 (--env-file 로 토큰 주입, /data 볼륨 공유)
#   5) 헬스체크 — 봇 로그에 polling 시작 흔적이 뜨는지 확인
#
# 안전장치:
#   - DB(/opt/lck-nospo/videos.sqlite)는 건드리지 않는다. fetch가 누적 갱신.
#   - .env 는 이미지가 아니라 볼륨 옆(/opt/lck-nospo/.env)에 둔다.
#
# 사용법:
#   deploy/deploy.sh              # 코드 배포 → 재빌드 → 봇 재기동 → 헬스체크
#   deploy/deploy.sh --no-build   # 재빌드 생략, 봇 재기동만 (급하게 재시작)
#   deploy/deploy.sh --fetch      # 재빌드 없이 수집 파이프라인 1회 실행 (MODE=fetch)
# ============================================================================
set -euo pipefail

# ---- 설정 (환경에 맞게) -----------------------------------------------------
HOST="${LCK_DEPLOY_HOST:-root@192.168.0.29}"      # Proxmox 호스트
HOST_IP="${HOST#*@}"                               # 배포 기록용 IP
CTID="${LCK_DEPLOY_CTID:-202}"                    # lck-nospo LXC (stock=201 과 분리)

IMAGE="lck-nospo"
CONTAINER="lck-nospo-bot"
SRC_DIR="/root/lck-nospo"                          # LXC 내 소스 트리
DATA_MOUNT="/opt/lck-nospo"                        # LXC 내 데이터 볼륨 → 컨테이너 /data
ENV_FILE="$DATA_MOUNT/.env"                        # LXC 내 토큰 파일

# 봇 컨테이너 run 옵션 (Docker-in-LXC 필수 옵션 포함).
RUN_OPTS="-d --name $CONTAINER \
  --network host \
  --security-opt apparmor=unconfined \
  --restart unless-stopped \
  -e MODE=bot \
  -e LCK_DB_PATH=/data/videos.sqlite \
  --env-file $ENV_FILE \
  -v $DATA_MOUNT:/data"

# ---- 인자 ------------------------------------------------------------------
DO_BUILD=1; FETCH_ONLY=0
for a in "$@"; do
  case "$a" in
    --no-build) DO_BUILD=0 ;;
    --fetch)    FETCH_ONLY=1 ;;
    *) echo "알 수 없는 옵션: $a"; exit 1 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
say() { echo -e "\n\033[1;36m▶ $*\033[0m"; }

# ---- 0. .env(토큰) 전송 -----------------------------------------------------
# 이미지에 굽지 않고 볼륨 옆에 두고 --env-file 로 주입한다.
say "[0/5] .env(토큰) 전송 → $ENV_FILE"
if [ ! -f "$ROOT/.env" ]; then
  echo "  ❌ $ROOT/.env 가 없습니다. cp .env.example .env 후 토큰을 채우세요."; exit 1
fi
ssh "$HOST" "pct exec $CTID -- mkdir -p $DATA_MOUNT"
# scp 는 LXC 안으로 직접 못 가므로 호스트 경유 → pct push.
TMP_ENV="/tmp/lck-env-$STAMP"
scp -q "$ROOT/.env" "$HOST:$TMP_ENV"
ssh "$HOST" "pct push $CTID $TMP_ENV $ENV_FILE && rm -f $TMP_ENV"

# ---- --fetch : 수집 1회만 하고 종료 ----------------------------------------
if [ "$FETCH_ONLY" -eq 1 ]; then
  say "[fetch] 수집 파이프라인 1회 실행 (MODE=fetch)"
  ssh "$HOST" "pct exec $CTID -- docker run --rm \
    --network host --security-opt apparmor=unconfined \
    -e MODE=fetch -e LCK_DB_PATH=/data/videos.sqlite \
    -v $DATA_MOUNT:/data $IMAGE"
  echo -e "\n\033[1;32m🎉 수집 완료 (봇은 무상태라 재시작 불필요)\033[0m"
  exit 0
fi

# ---- 1. 소스 배포 + 2. 빌드 -------------------------------------------------
if [ "$DO_BUILD" -eq 1 ]; then
  say "[1/5] 소스 전송 → $SRC_DIR"
  TMP_SRC="/tmp/lck-src-$STAMP"
  rsync -az --delete \
    --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '.pytest_cache' --exclude '.idea' \
    --exclude '*.sqlite*' \
    --exclude '.env' \
    "$ROOT/" "$HOST:$TMP_SRC/"
  ssh "$HOST" "tar -C $TMP_SRC -cf - . | pct exec $CTID -- bash -c 'rm -rf $SRC_DIR && mkdir -p $SRC_DIR && tar -C $SRC_DIR -xf -'; rm -rf $TMP_SRC"

  say "[2/5] 이미지 빌드 (docker build -t $IMAGE)"
  ssh "$HOST" "pct exec $CTID -- bash -c 'cd $SRC_DIR && docker build -f deploy/Dockerfile -t $IMAGE .'"
else
  say "[1-2/5] 소스 rsync/빌드 생략"
fi

# ---- 3. 봇 재기동 -----------------------------------------------------------
say "[3/5] 봇 컨테이너 재기동"
ssh "$HOST" "pct exec $CTID -- bash -c '
  docker rm -f $CONTAINER 2>/dev/null || true
  docker run $RUN_OPTS $IMAGE
'"

# ---- 4. 부트스트랩 대기 안내 ------------------------------------------------
say "[4/5] 최초 배포면 부트스트랩(일정 import + 초기 수집)에 수십 초~수 분 소요될 수 있음"

# ---- 5. 헬스체크 — 봇 로그에 polling 시작 흔적 확인 -------------------------
say "[5/5] 헬스체크 (봇 polling 시작 확인)"
ok=0
for i in $(seq 1 40); do
  if ssh "$HOST" "pct exec $CTID -- docker logs --tail 50 $CONTAINER 2>&1" | grep -q "봇 polling 시작"; then
    ok=1; echo "  ✅ 봇 polling 시작 확인 (${i}차 시도)"; break
  fi
  # 컨테이너가 죽었으면 즉시 실패
  running="$(ssh "$HOST" "pct exec $CTID -- docker inspect -f '{{.State.Running}}' $CONTAINER 2>/dev/null" || echo false)"
  if [ "$running" != "true" ]; then
    echo "  ❌ 컨테이너가 실행 중이 아님. 로그:"
    ssh "$HOST" "pct exec $CTID -- docker logs --tail 60 $CONTAINER" 2>&1 || true
    record_deploy failed
    exit 1
  fi
  echo "  ...부트스트랩/기동 대기 중 ${i}/40"; sleep 6
done

# ---- 배포 기록 (vimos/deployments.json) -------------------------------------
# 배포 사실을 공통 상태 파일에 남긴다(있을 때만). 없으면 조용히 건너뛴다.
record_deploy() {
  local status="$1"
  local recorder="$HOME/PycharmProjects/vimos/shared/bin/record_deploy.py"
  [ -f "$recorder" ] || return 0
  local commit commit_msg digest
  commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  commit_msg="$(git -C "$ROOT" log -1 --pretty=%s 2>/dev/null || echo '')"
  # 방금 기동한 이미지 digest (LXC 안에서 조회).
  digest="$(ssh "$HOST" "pct exec $CTID -- docker inspect -f '{{index .RepoDigests 0}}' $IMAGE 2>/dev/null" | sed 's/.*@//' || true)"
  python3 "$recorder" \
    --project "$IMAGE" --commit "$commit" --commit-msg "$commit_msg" \
    --status "$status" \
    ${digest:+--image-digest "$digest"} \
    --host "$HOST_IP / ct-bots (CTID $CTID)" || true
}

if [ "$ok" -ne 1 ]; then
  echo "  ⚠️  polling 시작 로그를 아직 못 봤습니다 (부트스트랩이 길 수 있음). 최근 로그:"
  ssh "$HOST" "pct exec $CTID -- docker logs --tail 60 $CONTAINER" 2>&1 || true
  record_deploy unknown
else
  record_deploy healthy
fi

echo -e "\n\033[1;32m🎉 배포 완료 — 봇 컨테이너: $CONTAINER\033[0m"
echo "   로그: ssh $HOST \"pct exec $CTID -- docker logs -f $CONTAINER\""
