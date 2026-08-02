# lck-nospo

LCK/VCT 등 e스포츠 경기를 **스포일러 없이** 다시볼 수 있는 플랫폼.

## 무엇을 푸는가

경기를 챙겨보지 못한 상황에서 VOD를 보려고 하면 결과를 미리 알게 되는 요소가 너무 많다.

- 썸네일에 MVP 선수, 챔피언, 점수가 노출됨
- 영상 길이만 봐도 "5꽉", "스윕" 여부가 드러남
- 추천 영상 / 댓글 / 하이라이트 제목 / 조회수가 결과를 암시
- BO5 중간 세트부터 보면 이미 스코어가 표시됨

`lck-nospo`는 이런 결과 유추 요소를 모두 제거한 상태로 VOD를 제공한다.

## 무엇을 하는가

1. **일정 수집** — Leaguepedia(fandom) MatchCalendar에서 LCK/VCT 일정을 수집
2. **VOD 매칭** — 매치업 + 날짜 기반으로 YouTube / 치지직 / SOOP VOD를 찾아 연결
3. **무스포 표시** — 매치업("GEN vs HLE")과 날짜만 보여주고 나머지 메타데이터는 모두 차단
4. **무스포 재생** — BO5 전체를 하나의 가상 타임라인으로 이어 재생, 진행바 / 세트 수 / 남은 길이 숨김
5. **랜덤 재생** — 결과를 모르는 매치 풀에서 무작위 매치업을 선택해 재생

## 무스포 보장 원칙

UI에 노출 가능한 정보:

- 매치업 (`GEN vs HLE`)
- 경기 날짜
- 리그 / 라운드

UI에 절대 노출하지 않는 정보:

- 썸네일, 영상 제목, 영상 길이, 조회수
- 댓글, 추천 영상, 좋아요 수
- MVP, 챔피언 픽, 세트 스코어, BO 진행 상태
- 진행바의 정확한 비율 (세트 경계 노출 방지)

## 현재 진척도

### 데이터 파이프라인 (완료)
- [x] Leaguepedia CSV/ICS 일정 export 수집 (`data/schedules/`에 시드)
- [x] 팀별 일정 필터링 스크립트 (`schedule/scripts/extract_team_schedule.py`)
- [x] VALARM 포함 iCalendar 변환 (`schedule/scripts/csv_to_ics.py`)
- [x] 일정 → SQLite `matches` 테이블 import (`schedule/scripts/import_schedule.py`)
- [x] 치지직 VOD 목록 수집 어댑터 (`vod_index/scripts/fetch_chzzk_vods.py`, backend 전용)
- [x] 영상 제목 파싱 분류 (`vod_index/scripts/classify_videos.py` → `video_classifications`)

### 텔레그램 봇 (1차 완료)
- [x] `/watch` — 팀 선택 키보드 (matches에 등장한 팀들을 2열 그리드로)
- [x] `/watch <팀>` — 오늘까지 최근 10경기 리스트 (날짜+매치업만, 영상 유무 숨김)
- [x] `/watch <MMDD> <팀>` / `/watch <MMDD> <팀A> <팀B>` — 5세트 × (하이라이트/풀영상) 슬롯 그리드. 빈 슬롯도 동일하게 노출.
- [x] `/schedule` — 팀 선택 키보드
- [x] `/schedule <팀>` — 오늘 이후 다가오는 10경기
- [x] `/help`, `/start` — 명령어 안내 + 하단 영구 키보드(`/watch` `/schedule` `/help`)

### 텔레그램 봇 (2차 — 구독/알림, 완료)
- [x] `subscriptions(chat_id, team, wins_only, created_at)`, `notifications(chat_id, match_id, sent_at)` 테이블 + `idx_subscriptions_team` 인덱스 추가. 봇 부팅 시 `init_schema`가 `CREATE IF NOT EXISTS`로 멱등 생성.
- [x] `/subscribe`(팀 선택 박스) / `/subscribe <팀>`(대문자 정규화 + matches 존재 검증 후 INSERT OR IGNORE), `/unsubscribe`(구독 중인 팀만 박스로) / `/unsubscribe <팀>`, `/subscriptions`(내 구독 목록)
- [x] 알림 워커 — `JobQueue.run_repeating(notify_worker, interval=300, first=5)`, 5분 주기. 경기 `start_at_kst + 4h` 도달 + `wins_only=0`인 매치당 사용자별 1회 발송. send 먼저 → 성공 시 `notifications` INSERT(실패 시 다음 tick 재시도). chat_id당 0.5s sleep.
- [x] 봇이 죽어있던 동안의 알림 누락 방지 — 부팅 후 첫 tick(5초 뒤)에서 누락분 일괄 처리.

### 텔레그램 봇 (3차 — 승팀 추출 + wins 옵션, 완료)
- [x] `vod_index/scripts/extract_winners.py` — 인터뷰 영상 제목(`<승팀> <닉> 인터뷰 | A vs B`) 정규식으로 첫 토큰을 승팀으로 추출해 `match_winners(match_date, team_a, team_b, winner, source, source_id, detected_at)` PK + `idx_mw_lookup` 인덱스로 적재. `--only-new`로 미처리 인터뷰만 처리하며 `INSERT OR IGNORE`로 멱등. 인터뷰 publish_date와 실제 경기일이 ±1일 어긋날 수 있어 워커 쿼리에서 윈도우로 흡수.
- [x] `/subscribe <팀> wins` — 승리 확정된 매치만 알림. 같은 팀이 이미 구독 중이면 wins_only 값을 덮어쓴다. 응답 본문/`/help` 양쪽에 ⚠ 무스포 경고 명시 ("알림이 오는 것 자체가 승리를 알려줍니다"). 워커는 `match_winners` JOIN UNION ALL 분기로 매치업 두 팀 순서 무관·match_date ±1일 윈도우로 발송.

### 미래 작업
- [ ] VOD Resolver 자동화 (세트 timestamp 자동 추출)
- [ ] 자체 무스포 redirect 페이지 (외부 영상 페이지 스포 차단)
- [ ] 무스포 플레이어 (BO5 이어보기, 진행바 숨김)
- [ ] 랜덤 재생 모드

## 디렉터리 구조

```
lck-nospo/
├── schedule/                       # 일정 영역
│   └── scripts/
│       ├── extract_team_schedule.py  # 팀별 일정 필터링 (CSV → CSV)
│       ├── csv_to_ics.py             # VALARM 포함 iCalendar 변환 (CSV → 진짜 .ics)
│       └── import_schedule.py        # CSV → SQLite import
├── vod_index/                      # VOD 인덱스 영역 (backend 전용)
│   └── scripts/
│       ├── fetch_chzzk_vods.py       # 치지직 VOD 목록 수집 (→ data/vods/videos.sqlite)
│       └── classify_videos.py        # 영상 제목 → 분류 태깅
├── notifier/                       # 알림 / 봇 영역
│   └── scripts/
│       └── telegram_bot.py         # 텔레그램 봇 (/watch ...)
├── data/                           # 모든 데이터 (공유)
│   ├── schedules/                  # 일정 원본 / 팀별 필터링 결과
│   │   ├── rounds-1-2.ics          # LCK 2026 Rounds 1-2 원본 CSV
│   │   └── <team>/                 # 팀별 필터링 결과
│   └── vods/                       # VOD 인덱스 DB (backend 전용)
│       └── videos.sqlite           # videos / video_classifications / matches / fetch_runs
├── .env.example                    # cp .env.example .env 후 토큰 채우기
├── AGENTS.md
└── README.md
```

**기능 디렉터리 규칙**: 새 스크립트는 영역별 디렉터리(`schedule/`, `vod_index/`, `notifier/`)의 `scripts/` 아래에 둔다. 데이터 파일은 모두 `data/` 하위에 둔다 (코드 도메인 이름과 헷갈리지 않도록).

## 일정 데이터 소스

Leaguepedia의 MatchCalendarExport를 사용한다.

- 검색 URL: <https://lol.fandom.com/wiki/Special:RunQuery/MatchCalendarExport?pfRunQueryFormName=MatchCalendarExport&MCE%5B1%5D=LCK+2026&wpRunQuery=&pf_free_text=>
  - `MCE[1]=LCK 2026` 처럼 시즌을 넣고 실행하면 그 시즌 경기가 `Subject,Start Date,Start Time` CSV로 나온다.
- 폼에서 토너먼트(예: `LCK 2026 Rounds 3-4`)를 선택하면 CSV/ICS export 가능
- 받은 파일은 `data/schedules/<라운드>.ics` 로 저장 (확장자는 `.ics`지만 내용은 CSV)

> ⚠️ **일정은 자동 갱신되지 않는다.** VOD 수집(`fetch`)은 cron이 돌지만, `matches`(경기 일정)는
> Leaguepedia에서 **사람이 새 라운드 .ics를 export해서 넣어줘야** 한다. 새 라운드가 시작되면
> 위 URL에서 export → `data/schedules/`에 저장 → 아래 "일정 갱신" 절차로 반영한다.
> 안 하면 봇의 `/watch`·`/schedule`이 옛 라운드에서 멈춘 것처럼 보인다.

### 일정 갱신 (새 라운드 반영)

새 .ics를 받은 뒤, **재배포 없이** 운영 중인 봇 DB에 바로 반영할 수 있다 (봇은 무상태):

```bash
# 로컬에서 파싱 검증 (임시 DB, 실 DB 안 건드림)
LCK_DB_PATH=/tmp/t.sqlite .venv/bin/python schedule/scripts/import_schedule.py data/schedules/rounds-3-4.ics

# 서버 볼륨으로 복사 후 컨테이너 안에서 import
scp data/schedules/rounds-3-4.ics root@192.168.0.29:/tmp/r.ics
ssh root@192.168.0.29 "pct push 202 /tmp/r.ics /opt/lck-nospo/rounds-3-4.ics"
ssh root@192.168.0.29 "pct exec 202 -- docker exec lck-nospo-bot python schedule/scripts/import_schedule.py /data/rounds-3-4.ics"
```

`import_schedule.py`는 upsert(멱등)라 같은 파일을 다시 넣어도 중복되지 않는다. 다음 재배포 때는
이미지에 동봉된 `data/schedules/*.ics` 전체를 부트스트랩에서 다시 import하므로, **커밋에 새 .ics를
포함시켜 두면** 이후 환경에서도 자동 반영된다.

> 리그 prefix(`LCK 2026 ...` → `LCK`)는 `schedule/scripts/import_schedule.py`의 `LEAGUE_PREFIXES`가
> 매핑한다. 매핑에 없는 새 대회는 조용히 스킵되므로, 새 리그가 들어오면 거기에 한 줄 추가한다.

## 사용법

처음 시작 → 봇 실행까지 한 흐름. 각 단계의 옵션·세부는 아래 "단계별 상세"에.

### 0. 셋업

```bash
# 의존성
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 토큰
cp .env.example .env
# .env 열어서 TELEGRAM_BOT_TOKEN=<BotFather에서 받은 값> 채우기
```

### 1. Quick Start — 새 환경에서 봇 띄우기

데이터 디렉터리(`data/vods/`)는 `.gitignore`로 제외돼 있으므로 새 환경에서는 비어 있다. 아래 4단계로 봇이 동작할 수 있는 상태가 된다.

```bash
# (1) Leaguepedia에서 받은 일정(.ics)이 data/schedules/<file>.ics 에 있어야 함
ls data/schedules/*.ics

# (2) 일정을 matches 테이블에 적재
.venv/bin/python schedule/scripts/import_schedule.py data/schedules/*.ics

# (3) 치지직 채널 VOD 수집 — LCK 정규시즌 + EWC/Road to EWC 두 채널
.venv/bin/python vod_index/scripts/fetch_chzzk_vods.py 9381e7d6816e6d915a44a13c0195b202  # LCK 공식
.venv/bin/python vod_index/scripts/fetch_chzzk_vods.py fce7c8735e0646e642007198a8875882  # EWC 공식 (RTE)

# (4) 영상 제목 파싱 → 분류
.venv/bin/python vod_index/scripts/classify_videos.py

# (5) 봇 실행 (polling, foreground)
.venv/bin/python notifier/scripts/telegram_bot.py
```

봇이 떴으면 텔레그램에서 `/watch 0523 DK BFX` 같은 식으로 호출. 매치 데이터는 `data/vods/videos.sqlite` 한 파일에 모두 들어 있고, 봇은 매 요청마다 SELECT만 한다 (무상태). 데이터 갱신 시 봇 재시작 불필요. 단, 봇 코드 자체가 바뀌면(스키마 추가, 워커 등록 등) 재시작 필요.

### 2. 운영 (지속 수집)

(3)·(4)를 cron으로 주기적으로 돌리면 신규 VOD가 자동 누적된다. cron 예시는 아래 "권장 수집 스케줄" 참조.

봇은 한 번 띄워두면 됨. 죽지 않게 systemd / tmux / screen / supervisord 중 편한 걸로 감싸기.

---

## 단계별 상세

### 팀별 일정 추출

```bash
python schedule/scripts/extract_team_schedule.py HLE
python schedule/scripts/extract_team_schedule.py T1 --source data/schedules/rounds-1-2.ics
```

결과는 `schedules/<team>/<team>-rounds-1-2.ics`에 CSV 포맷으로 저장된다.

### Google Calendar용 .ics 변환 (알람 포함)

`schedules/` 안의 CSV(.ics 확장자지만 내용은 CSV)를 진짜 iCalendar로 바꾼다. 각 경기에 VALARM이 박혀, 캘린더 기본 알림 설정과 무관하게 항상 알림이 울린다.

```bash
python schedule/scripts/csv_to_ics.py data/schedules/hle/hle-rounds-1-2.ics --alarm=-P1D
python schedule/scripts/csv_to_ics.py data/schedules/rounds-1-2.ics --alarm=-PT1H --duration 240
python schedule/scripts/csv_to_ics.py data/schedules/hle/hle-rounds-1-2.ics --alarm=none
```

알람 표기는 iCalendar `TRIGGER` 값: `-P1D`(1일 전), `-PT1H`(1시간 전), `-PT30M`(30분 전), `none`(알람 없음).

> argparse가 `-`로 시작하는 값을 옵션으로 오해하므로 **반드시 `--alarm=-P1D`처럼 `=`로 붙여 쓴다.**

출력 기본 경로는 입력 옆에 `<stem>-cal.ics`. KST(`Asia/Seoul`) 시간대로 출력되며, Google Calendar 가져오기 화면에서 그대로 import하면 된다.

### VOD 목록 수집 (backend 전용)

치지직 채널의 공개 VOD 목록을 가져와 공용 SQLite(`vods/videos.sqlite`)에 누적한다. DB는 소스 중립 스키마(`source`, `source_id`, `url`, `title`, `publish_date`)이고, 치지직 외 소스(YouTube/SOOP)가 같은 테이블에 추가 적재할 수 있다.

운영 중인 치지직 채널은 두 개다. 정규시즌 영상은 LCK 공식 채널, EWC/Road to EWC 토너먼트(`league='RTE'`) 영상은 별도 EWC 공식 채널에 올라온다. 두 채널 모두 fetch 대상에 포함시켜야 봇 그리드가 placeholder가 아닌 실제 영상으로 채워진다.

| 채널 | ID | 호스팅 영상 |
|---|---|---|
| LCK 공식 | `9381e7d6816e6d915a44a13c0195b202` | LCK 정규시즌 (Rounds 1-2 등) |
| EWC 공식 채널A | `fce7c8735e0646e642007198a8875882` | Road to EWC / RTE 토너먼트 |

```bash
# 증분 수집 (DB의 가장 최신 publish_date 이후만)
python vod_index/scripts/fetch_chzzk_vods.py 9381e7d6816e6d915a44a13c0195b202
python vod_index/scripts/fetch_chzzk_vods.py fce7c8735e0646e642007198a8875882

# 전체 재수집
python vod_index/scripts/fetch_chzzk_vods.py 9381e7d6816e6d915a44a13c0195b202 --full

# 명시적 컷오프
python vod_index/scripts/fetch_chzzk_vods.py 9381e7d6816e6d915a44a13c0195b202 --since 2026-05-01
```

매 실행은 `fetch_runs` 테이블에 기록된다(시작/종료 시각, 페이지 수, 신규/업데이트 건수, status, error). cron이나 `/loop`으로 주기 실행하면 신규 VOD만 누적된다.

#### 권장 수집 스케줄 (cron)

LCK 정규 일정 기준: 수~일 17~24시 KST에 영상이 몰리고, 월/화는 휴방. 경기 시간엔 빠르게(7분), 그 외엔 느슨하게(1시간) 도는 2-룰 구성을 권장한다.

**`fetch` 직후 항상 `classify --only-new` + `extract_winners --only-new`를 같이 돌린다.** fetch만 돌리면 신규 영상이 `videos`엔 들어가지만 `video_classifications` / `match_winners`에는 안 들어가서 봇 그리드 및 `wins_only` 알림이 안 잡힌다. 두 채널 모두 fetch한 뒤 마지막에 classify/winners를 한 번씩 돌리는 게 효율적이라 wrapper 스크립트(`bin/run_fetch_pipeline.sh`)에 묶어 두었다. cron/LaunchAgent 한 줄에서는 wrapper만 호출.

```cron
# 경기 시간대: 수~일 17~23시, 7분마다
*/7 17-23 * * 3-7  /path/to/lck-nospo/bin/run_fetch_pipeline.sh

# 안전망: 매일 00~16시 정각 + 월/화 매시간 (늦은 업로드 / 휴방일 커버)
0 0-16 * * *       /path/to/lck-nospo/bin/run_fetch_pipeline.sh
0 * * * 1,2        /path/to/lck-nospo/bin/run_fetch_pipeline.sh
```

`bin/run_fetch_pipeline.sh`는 두 채널(LCK 공식 + EWC 공식)을 순차 fetch한 뒤 `classify_videos.py --only-new`와 `extract_winners.py --only-new`를 돌리고 `/tmp/chzzk-fetch.log`에 누적 기록한다. 한 채널 fetch가 실패해도 다음 채널은 시도하므로 부분 장애에 강하다.

- 요일 번호: 일=0/7, 월=1, ..., 토=6 (Linux/macOS 공통)
- 증분 수집이라 신규 0건일 땐 page 1만 보고 종료하므로 부하 거의 없음. `classify --only-new`도 미분류 0건이면 즉시 종료.
- 실패 감지: `SELECT * FROM fetch_runs WHERE status='failed' ORDER BY started_at DESC LIMIT 10`

#### macOS LaunchAgent로 운영 (현재 방식)

운영 환경이 맥인 경우 cron 대신 `launchd`를 사용한다. cron과 달리 시스템이 sleep 중일 때 놓친 트리거를 깨어났을 때 따라잡아 실행한다. plist 3개가 `~/Library/LaunchAgents/com.invidam.lck-nospo.fetch-{gameday,hourly,montue}.plist`에 등록돼 있다. 모두 `bin/run_fetch_pipeline.sh`를 호출한다.

```bash
# 등록 확인
launchctl list | grep lck-nospo

# 재로드 (plist 수정 후)
launchctl bootout gui/$(id -u)/com.invidam.lck-nospo.fetch-gameday
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.invidam.lck-nospo.fetch-gameday.plist
```

**⚠️ backend 전용**: `videos.title`에는 영상 제목 원문이 들어가므로 결과 유추가 가능하다. 프론트엔드로 직접 노출하지 말 것.

### 영상 분류 (videos → video_classifications)

수집한 영상의 제목을 정규식으로 파싱해 별도 테이블에 매치/세트/유형을 박는다. 원본 `videos`는 안 건드린다 (소스 어댑터와 책임 분리).

```bash
python vod_index/scripts/classify_videos.py             # 전체 재분류
python vod_index/scripts/classify_videos.py --only-new  # 미분류만
```

분류 kind:
- `full` — 세트 풀VOD (제목에 "게임 N VOD")
- `set_highlight` — 세트별 하이라이트 ("게임 N 하이라이트")
- `match_full` / `match_highlight` — 매치 전체 풀/하이라이트 (스포 위험 ↑, 봇 노출 보류)
- `excluded` — 인터뷰/예고/이벤트

매 실행은 `classify_runs` 테이블에 기록한다(`fetch_runs`와 동일 패턴). cron으로 `fetch && classify --only-new`를 묶어 돌릴 때 분류 단계 실패가 fetch 성공에 묻히지 않게, 분류 측 로그가 별도로 있어야 무성 실패를 잡을 수 있다.

```sql
classify_runs(
  id, mode,                      -- mode: 'full' | 'only_new'
  started_at, finished_at,
  scanned_count,                 -- 이번 실행에서 검토한 videos 행 수
  classified_count,              -- 새로 분류된 행 수
  excluded_count,                -- kind='excluded'로 처리된 행 수
  status, error
)
```

- 실패 감지: `SELECT * FROM classify_runs WHERE status='failed' ORDER BY started_at DESC LIMIT 10`
- 무성 실패 감지: 같은 시간대 `fetch_runs.new_count > 0`인데 `classify_runs.classified_count = 0`이면 정규식이 새 제목 포맷을 못 잡고 있을 가능성 → 표본 확인 후 패턴 보강

### 승팀 추출 (videos → match_winners)

치지직 인터뷰 영상 제목 패턴 `<승팀> <닉> 인터뷰 <구분자> <팀A> vs <팀B>`에서 첫 토큰을 승팀으로 뽑아 `match_winners`에 적재한다. 첫 토큰이 매치업 두 팀에 포함되지 않으면 스킵.

구분자는 채널마다 다르다. **LCK 공식 채널은 ASCII `|`**, **EWC 공식 채널은 한글 `ㅣ` (U+3163)** 를 쓴다 (`HLE Kanavi 인터뷰ㅣHLE vs BROㅣRoad to EWC 2026`). 정규식은 `[ㅣ|]` 문자 클래스로 둘 다 잡는다. 새 채널을 추가해 또 다른 구분자가 나오면 이 문자 클래스에 추가.

```bash
python vod_index/scripts/extract_winners.py             # 전체 재추출
python vod_index/scripts/extract_winners.py --only-new  # 미처리 인터뷰만
```

`INSERT OR IGNORE`로 멱등 (PK: `match_date, team_a, team_b, source, source_id`). `match_date`는 인터뷰의 publish_date를 KST 자정으로 잘라 저장하며, 실제 경기일과 ±1일 차이는 워커 쿼리의 윈도우 JOIN으로 흡수한다.

매 실행은 `winners_runs` 테이블에 기록한다.

```sql
winners_runs(
  id, mode,                      -- mode: 'full' | 'only_new'
  started_at, finished_at,
  scanned_count,                 -- 검토한 인터뷰 영상 행 수
  matched_count,                 -- 매치업 두 팀에 첫 토큰이 매칭된 건수
  skipped_count,                 -- 첫 토큰이 매치업과 안 맞아 스킵된 건수
  inserted_count,                -- match_winners에 새로 들어간 행 수
  status, error
)
```

- 실패 감지 + 알림 누락 원인 추적: `/subscribe wins` 구독자가 알림을 못 받았다고 신고하면 같은 시간대 `winners_runs.scanned_count` / `matched_count` / `skipped_count`로 인터뷰 자체가 없었는지 / 토큰 파싱이 깨졌는지를 분리해서 본다.

**`/subscribe <팀> wins` 구독자 알림은 이 데이터에 전적으로 의존한다.** 인터뷰가 안 올라온 매치는 영영 안 보내짐.

### 일정 → matches 적재

```bash
python schedule/scripts/import_schedule.py data/schedules/rounds-1-2.ics
python schedule/scripts/import_schedule.py data/schedules/rounds-1-2.ics --reset  # wipe 후 재적재
```

`matches` 테이블 (`match_id`, `league`, `season`, `stage`, `team_a`, `team_b`, `start_at_kst`, `match_date`)에 정규화 적재. 봇의 `/watch`, `/team`, `/schedule`이 이 테이블 조회.

### 텔레그램 봇

실행은 위 Quick Start (5) 참조. 봇은 `data/vods/videos.sqlite`만 읽는 **무상태 polling 프로세스**다. 데이터 갱신 시 재시작 불필요.

봇 명령어:

| 명령 | 동작 |
|---|---|
| `/watch` | 팀 선택 키보드 |
| `/watch <팀>` | 최근 10경기 (날짜+매치업만, 영상 유무 숨김) |
| `/watch <MMDD> <팀>` | 그 날 그 팀이 낀 매치 (결과 여러 개면 매치 선택 화면) |
| `/watch <MMDD> <팀A> <팀B>` | 특정 매치업 → 5세트 × (하이라이트/풀영상) 그리드 |
| `/schedule` | 팀 선택 키보드 |
| `/schedule <팀>` | 오늘 이후 다가오는 10경기 |
| `/subscribe` | 팀 선택 박스 (matches에 등장한 모든 팀) |
| `/subscribe <팀>` | 구독 추가 — 승패 모두 알림 (대문자 정규화 + 존재 검증) |
| `/subscribe <팀> wins` | 구독 추가 — 이겼을 때만 알림 (⚠ 알림이 오는 것 자체가 승리 노출) |
| `/unsubscribe` | 구독 중인 팀만 박스로 (0개면 안내) |
| `/unsubscribe <팀>` | 구독 해제 |
| `/subscriptions` | 내 구독 목록 |
| `/help`, `/start` | 명령어 안내 + 하단 영구 키보드 |

- 날짜는 MMDD 4자리. 연도는 KST 현재 연도 자동, 미래면 작년.
- 인라인 키보드는 **항상 5세트 × 2종 고정**. 영상이 없는 슬롯도 동일한 외관·URL button(`https://chzzk.naver.com/video/1` placeholder)으로 그린다 — 슬롯 유무로 시리즈 종료를 추정할 수 없게 하기 위함.

### 의존성

`requirements.txt`에 명시. 현재는 `python-telegram-bot` 하나뿐 (수집/분류 스크립트는 표준 라이브러리만 사용).

```bash
.venv/bin/pip install -r requirements.txt
```

## 아키텍처 개요

```
┌──────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│ Leaguepedia  │  │ 치지직 / YT / SOOP│  │ 사용자 (텔레그램)    │
│ CSV export   │  │ (영상 메타데이터) │  │                      │
└──────┬───────┘  └────────┬─────────┘  └──────────┬───────────┘
       │                   │                       │
       ▼                   ▼                       ▼
┌──────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│ import_      │  │ fetch_chzzk_vods │  │ telegram_bot.py      │
│ schedule.py  │  │ + classify_videos│  │ (무상태, polling)    │
└──────┬───────┘  └────────┬─────────┘  └──────────┬───────────┘
       │                   │                       │
       └────┬──────────────┘                       │
            ▼                                      │
   ┌─────────────────────────────────┐             │
   │  data/vods/videos.sqlite        │◀────────────┘
   │  - matches                      │  매 요청마다 SELECT
   │  - videos                       │
   │  - video_classifications        │
   │  - fetch_runs                   │
   └─────────────────────────────────┘
```

수집(`fetch_*`, `classify_*`, `import_*`)은 cron으로 돌고, 봇은 SQLite를 매 요청마다 읽는 무상태 프로세스다. 데이터 갱신 시 봇 재시작 불필요.

**무스포 경계**: `videos.title` 같은 결과 유추 가능 필드는 봇 응답에 직접 들어가지 않는다. 봇은 매칭된 `video_id`와 URL만 사용자에게 노출한다.

**시각적 균일성**: 5세트 그리드의 모든 버튼은 URL 버튼이다. 영상이 없는 슬롯도 placeholder URL로 채워 색·모양을 동일하게 유지한다 — callback 버튼은 텔레그램 클라이언트에서 URL 버튼과 색이 달라 영상 유무가 들키기 때문. 도착 외부 페이지의 스포 차단(추천/댓글)은 별개 마일스톤(자체 redirect 페이지).

## 기술 리스크

- **YouTube ToS** — video URL 직접 추출은 금지. embed iframe + CSS 마스킹으로 추천 영상 / 제목 차단
- **VOD 매칭 정확도** — 자동 검색은 오매칭 / 스포일러 영상 혼입 위험. 초기에는 LCK 공식 채널 등 화이트리스트로 시작
- **세트 구간 검출** — 영상 내 UI OCR 또는 YouTube 챕터 활용. 초기에는 수동 큐레이션이 현실적
