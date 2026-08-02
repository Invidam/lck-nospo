# 배포 가이드 (Docker) — `deploy/` 하나로 완결

리눅스 서버에서 **텔레그램 봇을 상시** 띄우고, **VOD 링크 인덱스를 주기적으로** 갱신한다.
사람이 손댈 필요 없이 서버가 알아서 돌고, 사용자는 **텔레그램**으로 봇을 쓴다.

> 자매 프로젝트 `stock-routine/deploy` 와 같은 패턴(단일 이미지 + `MODE` 분기 +
> Docker-in-LXC)이다. 두 프로젝트 공통 규칙(정본)은
> `~/PycharmProjects/vimos/shared/CONVENTIONS.md` 참고 (AGENTS.md 가 import).

## 두 컴포넌트 (모두 서버 컨테이너에서)

| 역할 | 실행 | 스크립트 |
|------|------|----------|
| 텔레그램 봇 (상시 polling) | `MODE=bot` | `notifier/scripts/telegram_bot.py` |
| VOD 링크 수집 (주기 배치) | `MODE=fetch` | `bin/run_fetch_pipeline.sh` 상당의 3단계 |

VOD 수집 배치는 **치지직 채널의 공개 VOD 목록(제목·URL·게시일)을 긁어와 DB에 쌓는**
인덱싱 작업이다. 영상 파일을 받는 게 아니라 링크/메타만 모은다. 3단계로 구성:

1. `fetch_chzzk_vods.py` — 두 채널(LCK 공식 + EWC 공식)의 신규 VOD 목록 수집
2. `classify_videos.py --only-new` — 제목 파싱 → 세트/유형 분류
3. `extract_winners.py --only-new` — 인터뷰 제목에서 승팀 추출 (wins 알림용)

`entrypoint.sh` 가 둘을 `MODE` 로 묶는다:
- `MODE=bot` (기본): (최초면 부트스트랩) → 봇 상시 polling
- `MODE=fetch`: 수집 파이프라인 1회 실행 후 종료 (cron 배치용)

**부트스트랩**: 볼륨의 DB가 비어 있으면(최초 배포) 봇을 띄우기 전에
이미지 동봉 `data/schedules/*.ics` 를 `matches` 로 import 하고 수집을 1회 돌린다.
DB가 이미 있으면 건너뛴다.

---

## 1. 이미지 빌드 (컨텍스트 = 프로젝트 루트)

```bash
cd lck-nospo
docker build -f deploy/Dockerfile -t lck-nospo .
```

## 2. 봇 컨테이너 상시 실행

```bash
docker run -d --name lck-nospo-bot \
  --restart unless-stopped \
  -e MODE=bot \
  -e LCK_DB_PATH=/data/videos.sqlite \
  --env-file /opt/lck-nospo/.env \
  -v /opt/lck-nospo:/data \
  lck-nospo
```
- DB는 호스트 `/opt/lck-nospo` 볼륨의 `videos.sqlite` 에 영속화(컨테이너 재생성돼도 유지).
- **토큰(`TELEGRAM_BOT_TOKEN`)은 이미지에 굽지 않는다.** `/opt/lck-nospo/.env` 에 두고
  `--env-file` 로 런타임 주입한다(`.dockerignore` 가 `.env` 를 빌드에서 제외).
- 봇은 무상태(SELECT만)라 데이터 갱신 시 재시작 불필요. 봇 **코드**가 바뀌면 재기동.

## 3. 매 수집 자동화 (핵심) — 호스트 cron

**`deploy/crontab`** 을 서버(LXC) crontab 으로 등록하면 끝이다.

```bash
crontab deploy/crontab     # 등록
crontab -l                 # 확인
```

- 경기 시간대(수~일 17~23시 KST)엔 7분마다, 그 외엔 느슨하게 `MODE=fetch` 컨테이너가
  떠서 신규 VOD 를 수집하고 `--rm` 으로 종료한다.
- 증분 수집이라 신규 0건이면 page 1만 보고 끝나 부하 거의 없음.
- 치지직 조회를 위해 **컨테이너에 인터넷 접속 필요**.

> 스케줄 근거: LCK 경기는 수~일 17~24시에 몰리고 월/화는 휴방(README 참조).

---

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `MODE` | `bot` | `bot`=봇 상시 / `fetch`=수집 1회 후 종료 |
| `LCK_DB_PATH` | `/data/videos.sqlite` | DB 경로(볼륨). 봇·수집 스크립트가 이 변수를 읽음 |
| `TELEGRAM_BOT_TOKEN` | — | BotFather 토큰. `--env-file` 로 주입 |
| `CHZZK_CHANNEL_LCK` | `9381e7d6816e6d915a44a13c0195b202` | LCK 공식 채널 |
| `CHZZK_CHANNEL_EWC` | `fce7c8735e0646e642007198a8875882` | EWC 공식 채널(RTE) |

## 로그 / 중지

```bash
docker logs -f lck-nospo-bot
docker stop lck-nospo-bot && docker rm lck-nospo-bot
tail -f /var/log/lck-nospo-fetch.log   # 수집 배치 로그
```

---

## 실전 배포 (현재 운영 환경) — `deploy/deploy.sh`

위 1~3은 "일반 도커 서버" 기준이다. 실제 운영은 **Proxmox 노트북 안의 LXC 에서
Docker-in-LXC** 로 돌린다(stock-routine 과 같은 호스트, 다른 LXC/컨테이너로 분리).
매번 손으로 rsync·build·run 하지 않도록 **`deploy/deploy.sh`** 하나로 묶었다.

### 운영 환경 좌표

| 항목 | 값 |
|------|-----|
| Proxmox 호스트 | `root@192.168.0.29` |
| LXC | CTID `202` (stock=`201` 과 분리) |
| 이미지 / 컨테이너 | `lck-nospo` / `lck-nospo-bot` |
| 소스 트리(LXC 내) | `/root/lck-nospo` |
| DB 볼륨(LXC 내) | `/opt/lck-nospo` → 컨테이너 `/data` (`videos.sqlite`) |
| 토큰 파일(LXC 내) | `/opt/lck-nospo/.env` → `--env-file` |
| run 옵션 | `--network host --security-opt apparmor=unconfined --restart unless-stopped` |

> `--network host` 와 `apparmor=unconfined` 는 Docker-in-LXC 필수 옵션이다
> (없으면 sysctl / AppArmor 에러). `deploy.sh` 에 이미 반영돼 있다.

### 배포 명령

```bash
./deploy/deploy.sh              # 코드 배포 → 재빌드 → 봇 재기동 → 헬스체크
./deploy/deploy.sh --no-build   # 재빌드 생략, 봇 재기동만 (급하게 재시작)
./deploy/deploy.sh --fetch      # 재빌드 없이 수집 파이프라인 1회 실행 (MODE=fetch)
```

`deploy.sh` 가 하는 일 (순서대로):
1. **`.env`(토큰) 전송** → `/opt/lck-nospo/.env` (이미지엔 안 굽고 런타임 주입).
2. **소스 rsync** → `/root/lck-nospo` (`.venv/.git/DB/.env/.idea` 제외).
3. **`docker build`** → 이미지 `lck-nospo`.
4. **봇 컨테이너 재기동** — 위 표의 run 옵션 그대로.
5. **헬스체크** — 봇 로그에 "봇 polling 시작" 이 뜰 때까지 폴링(컨테이너 죽으면 로그 출력 후 exit 1).

환경이 다르면 아래 환경변수로 덮어쓴다:
`LCK_DEPLOY_HOST`, `LCK_DEPLOY_CTID`.

### 자주 쓰는 시나리오

| 뭘 바꿨나 | 명령 |
|-----------|------|
| 봇/수집 파이썬 코드 수정 | `./deploy/deploy.sh` (재빌드 필요) |
| 지금 당장 VOD 한 번 더 긁고 싶다 | `./deploy/deploy.sh --fetch` |
| 급하게 봇만 재기동 | `ssh root@192.168.0.29 "pct exec 202 -- docker restart lck-nospo-bot"` |

### 데이터 갱신 시 안전성

수집 스크립트는 모두 증분 + 멱등이다. `fetch` 는 DB 최신 `publish_date` 이후만
가져오고, `classify`/`extract_winners` 는 `--only-new` 로 미처리분만,
`import_schedule` 는 upsert 라 같은 `.ics` 를 다시 넣어도 중복되지 않는다.
봇은 무상태라 수집 후 재시작이 필요 없다.
