# ============================================================================
# lck-nospo 단일 이미지 — 봇(상시) + VOD 수집(배치)을 모두 담는다.
# 실행 분기는 entrypoint.sh 의 MODE 환경변수로 처리한다 (bot / fetch).
#
# 빌드는 반드시 "프로젝트 루트"를 컨텍스트로:
#   docker build -f deploy/Dockerfile -t lck-nospo .
# ============================================================================
FROM python:3.11-slim

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 전체 복사 (.dockerignore 로 .venv/.git/DB/.env 등은 제외됨).
# data/schedules/*.ics 는 포함되어 최초 부트스트랩 때 matches 를 시드한다.
COPY . .

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# DB는 볼륨에 영속화 (컨테이너 재생성돼도 유지)
ENV LCK_DB_PATH=/data/videos.sqlite \
    MODE=bot \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
