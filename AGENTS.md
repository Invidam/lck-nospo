# AGENTS.md

`lck-nospo` 저장소에서 코딩 에이전트가 작업할 때 따라야 할 가이드.

## 프로젝트 한 줄 요약

LCK/VCT VOD를 **스포일러 없이** 다시볼 수 있게 해주는 플랫폼. 결과를 유추할 수 있는 모든 요소(썸네일, 길이, 점수, MVP, 댓글, 추천)를 제거한다.

## 절대 규칙 — 무스포 우선

코드 변경 / 기능 제안 시 항상 다음을 1순위로 검토한다.

1. **노출 가능 정보** — 매치업(`A vs B`), 날짜, 리그/라운드. 끝.
2. **노출 금지 정보** — 영상 제목, 썸네일, 영상 길이, 조회수, 댓글, 추천 영상, MVP, 챔피언, 세트 스코어, BO 진행 상태, 진행바의 정확한 비율
3. **백엔드 ↔ 프론트엔드 경계** — 외부 API(YouTube / 치지직 / SOOP)에서 받은 메타데이터는 매칭 용도로만 쓰고 프론트엔드에 전달 금지. 프론트엔드에 가는 데이터는 `{match_id, video_id, set_offsets}` 같은 최소 식별자만.
4. **로그 / 에러 메시지** — 디버깅 로그에 영상 제목, 길이를 출력할 때도 사용자에게 노출될 수 있는 경로(예: 에러 페이지, sentry 공유)를 고려한다.

신규 기능을 만들 때 "이 정보가 사용자에게 보이는가? 보인다면 결과를 유추할 수 있는가?" 를 먼저 자문할 것.

## 코드 스타일

- Python 3.11+ 가정 (`from __future__ import annotations` 사용)
- 파일 헤더 docstring에 한국어 사용법 예시 포함 (`schedule/scripts/extract_team_schedule.py` 참고)
- 에이전트가 작성하는 코드는 **주석 최소화**. 식별자 이름으로 의도가 드러나지 않는 경우(숨겨진 제약, 외부 API의 비직관적 동작)에만 한 줄 주석.
- 사용자 메시지 / CLI 출력은 한국어. 코드 식별자는 영어.
- 스크립트는 기능 디렉터리(`schedule/`, `vod_index/`, `notifier/`)의 `scripts/` 아래에 둔다. 루트나 공용 `scripts/`에 두지 않는다.

## 디렉터리 관습

프로젝트는 3개 기능 도메인으로 나뉘고, 각 도메인은 자기 `scripts/`를 가진다.
데이터는 도메인이 공유하며 **모두 `data/` 아래에 둔다** — 코드 도메인 이름(`schedule`, `vod_index`)과 데이터 디렉터리(`schedules/`, `vods/`)가 한 글자 차이로 헷갈리지 않도록.

```
schedule/                   # ① 캘린더 뽑기/변환/DB 정규화 적재 (코드)
└── scripts/
    ├── extract_team_schedule.py   # CSV → 팀별 CSV
    ├── csv_to_ics.py              # CSV → 진짜 iCalendar (+ VALARM)
    └── import_schedule.py         # CSV → data/vods/videos.sqlite::matches

vod_index/                  # ② VOD 메타데이터 수집/분류 (코드, backend 전용)
└── scripts/
    ├── fetch_chzzk_vods.py        # 치지직 → videos
    └── classify_videos.py         # videos → video_classifications

notifier/                   # ③ DB+일정 기반 알람 / 텔레그램 봇 (코드)
└── scripts/
    └── telegram_bot.py            # /watch [<date>] [<team>...] → 매치 검색 / 5세트 슬롯 인라인 키보드

data/                       # 모든 데이터 (공유)
├── schedules/              # 일정 데이터
│   ├── <source-file>.ics   # Leaguepedia에서 받은 원본 (실제로는 CSV 포맷)
│   └── <team>/             # 팀별로 필터링한 결과
│       └── <team>-<round>.ics
├── vods/                   # VOD 데이터 (backend 전용)
│   └── videos.sqlite       # 소스 중립 VOD 인덱스 + 일정 정규화 테이블
└── identifier.sqlite       # (TBD)
```

- 팀 디렉터리 이름은 소문자 약어 (`hle`, `t1`, `gen`)
- 새 파일을 받을 때는 `rounds-<N>-<M>.ics`(`rounds` 정자) 로 저장
- `data/vods/` 아래 파일은 **backend 전용**이다. 프론트엔드에 직접 전달 금지.
- 새 도메인이 생기면 같은 패턴(`<domain>/scripts/`)을 따른다. 공용 코드가 3번째로 중복될 때 `common/` 분리를 고려.
- **데이터 디렉터리는 항상 `data/` 하위**다. 루트에 `schedules/`, `vods/`, `*.sqlite`를 두지 않는다.

## VOD 인덱스 스키마

`data/vods/videos.sqlite`는 소스 중립적이다. 치지직 / YouTube / SOOP 등 어떤 소스든 같은 테이블에 적재한다. 어댑터 스크립트는 `vod_index/scripts/fetch_<source>_vods.py` 형태로 추가한다.

```sql
videos(
  source, source_id,           -- PK. source 예: 'chzzk', 'youtube'
  url, title, publish_date,    -- 매칭에 필요한 최소 필드
  first_seen, last_seen        -- 운영용 timestamp
)

fetch_runs(
  id, source, source_key,      -- source_key는 채널 식별자 (소스마다 형식 자유)
  started_at, finished_at,
  pages_fetched, new_count, updated_count,
  cutoff, status, error
)

classify_runs(
  id, mode,                    -- mode: 'full' | 'only_new'
  started_at, finished_at,
  scanned_count, classified_count, excluded_count,
  status, error
)

match_winners(
  match_date, team_a, team_b,  -- PK 일부. match_date는 인터뷰 publish_date의 KST 자정
  winner,                      -- 인터뷰 제목 첫 토큰
  source, source_id,           -- PK 마무리. videos와 동일한 (source, source_id)
  detected_at
)
-- PK (match_date, team_a, team_b, source, source_id)
-- INDEX idx_mw_lookup(match_date, team_a, team_b, winner)

winners_runs(
  id, mode,                    -- mode: 'full' | 'only_new'
  started_at, finished_at,
  scanned_count,               -- 검토한 인터뷰 영상 행 수
  matched_count,               -- 첫 토큰이 매치업 두 팀 중 하나와 매칭된 건수
  skipped_count,               -- 첫 토큰이 매치업과 안 맞아 스킵된 건수
  inserted_count,              -- match_winners에 새로 들어간 행 수
  status, error
)
```

- 새 소스 어댑터를 만들 때 **이 스키마를 확장하지 말 것.** 소스별 필드(영상 길이/카테고리/태그 등)가 필요해지면 별도 테이블에 `(source, source_id)` 외래키로 붙인다.
- `title`은 결과 유추 가능 필드다. 로그·에러 메시지에 그대로 노출 주의.
- `fetch_runs` / `classify_runs` / `winners_runs`는 같은 패턴의 운영 로그 테이블이다. 새 파이프라인 스크립트를 추가할 때도 `<name>_runs` 규약으로 둔다. **무성 실패** 추적이 핵심 용도 — fetch 신규 > 0인데 classify 분류 = 0, 인터뷰 scan > 0인데 winners inserted = 0 같은 케이스.

### 현재 시드 데이터
`data/vods/videos.sqlite`에는 두 치지직 채널의 VOD가 적재된다.
- LCK 공식 (`9381e7d6816e6d915a44a13c0195b202`) — LCK 정규시즌. 제목 구분자 ASCII `|`, 세트 표기 `게임 1` (공백 있음).
- EWC 공식 채널A (`fce7c8735e0646e642007198a8875882`) — Road to EWC / RTE 토너먼트. 제목 구분자 한글 `ㅣ` (U+3163), 세트 표기 `게임1` (공백 없음).

수집/분류/승팀 추출 스크립트는 두 포맷을 모두 인식한다. 새 채널을 추가하다 매칭이 0건이면 우선 제목 구분자와 세트 표기 공백 유무부터 확인할 것.

치지직 채널 영상 제목 패턴 예시 (LCK 공식, ASCII `|`):
- `<팀A> vs <팀B> 게임 N VOD | MM.DD | 2026 LCK` — 전체 세트 풀영상
- `<팀A> vs <팀B> 매치 NN 하이라이트 | 2026 LCK` — 매치 하이라이트
- `<팀A> vs <팀B> 게임 N 하이라이트 | 2026 LCK` — 세트별 하이라이트
- `<선수> 인터뷰 | <팀A> vs <팀B> | 2026 LCK` — 인터뷰
- `Week N <Day> Showdown : <팀A> vs <팀B> | 2026 LCK` — 프리뷰

EWC 공식 채널 (한글 `ㅣ`):
- `<팀A> vs <팀B> 게임N VODㅣMM.DDㅣRoad to EWC 2026` — 풀영상
- `<팀A> vs <팀B> 게임N 하이라이트ㅣMM.DDㅣRoad to EWC 2026` — 세트 하이라이트
- `<승팀> <선수> 인터뷰ㅣ<팀A> vs <팀B>ㅣRoad to EWC 2026` — 인터뷰

## 데이터 소스

- **일정** — Leaguepedia(`lol.fandom.com`) MatchCalendarExport
  - 폼: `Special:RunQuery/MatchCalendarExport`
  - export 형식이 `.ics`로 다운로드되지만 실제 내용은 CSV (`Subject, Start Date, Start Time` 컬럼)
  - `Subject` 형식: `<리그> <라운드> - <팀A> vs <팀B>`
- **VOD** — YouTube, 치지직, SOOP. Resolver는 backend 전용.

## 에이전트가 자주 할 작업

### 새 환경 부트스트랩 (DB 비어있을 때)
사용자가 새 머신에 체크아웃했거나 `data/vods/`가 비어있는 상태일 때 다음 순서.

```bash
# 0. 의존성 (python-telegram-bot[job-queue] extras로 apscheduler/tzlocal까지 자동 설치)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # TELEGRAM_BOT_TOKEN 채우기

# 1. 일정 적재 (data/schedules/*.ics가 이미 있어야 함)
.venv/bin/python schedule/scripts/import_schedule.py data/schedules/*.ics

# 2. VOD 수집 + 분류 + 승팀 추출
.venv/bin/python vod_index/scripts/fetch_chzzk_vods.py 9381e7d6816e6d915a44a13c0195b202
.venv/bin/python vod_index/scripts/classify_videos.py
.venv/bin/python vod_index/scripts/extract_winners.py

# 3. 봇 실행
.venv/bin/python notifier/scripts/telegram_bot.py
```

- `data/schedules/`가 비어있으면 먼저 Leaguepedia에서 .ics(실제론 CSV) export 받아 거기에 둔다.
- 각 단계 스크립트는 idempotent — 다시 돌려도 중복 없이 신규/업데이트만 처리.
- 자세한 사용자 가이드는 README "사용법" 참조.

### 새 라운드 일정 추가
1. Leaguepedia에서 CSV export 받아 `data/schedules/<source>.ics`로 저장
2. `schedule/scripts/extract_team_schedule.py`로 팀별 필터링
3. 정규화 스키마가 생기면 그쪽에도 반영

### 새 팀 처리
`schedule/scripts/extract_team_schedule.py`는 팀 약어를 받아 동작한다. Leaguepedia의 약어와 일치해야 한다(`HLE`, `GEN`, `T1`, `KT`, `DK` 등 대문자 영숫자).

### Google Calendar용 .ics(알람 포함) 변환
`schedule/scripts/csv_to_ics.py`로 CSV → 진짜 iCalendar 변환. 각 VEVENT에 VALARM(기본 `-P1D`, 1일 전)이 박힌다. 캘린더 기본 알림과 무관하게 알림이 보장된다. argparse 특성상 `--alarm=-P1D`처럼 `=`로 붙여서 전달해야 한다.

### VOD 목록 수집
소스별 어댑터 스크립트(`vod_index/scripts/fetch_<source>_vods.py`)로 공용 DB(`data/vods/videos.sqlite`)에 누적한다. 기본 동작은 증분 수집(`MAX(publish_date)` 이후만). 매 실행은 `fetch_runs`에 기록된다. cron / `/loop`으로 주기 실행 가정.

- 치지직: `vod_index/scripts/fetch_chzzk_vods.py <CHANNEL_ID>` (공개 API, 인증 불필요)
- 채널 ID:
  - LCK 공식 (정규시즌): `9381e7d6816e6d915a44a13c0195b202`
  - EWC 공식 (RTE/Road to EWC): `fce7c8735e0646e642007198a8875882`

운영 스케줄은 `bin/run_fetch_pipeline.sh` wrapper로 묶어 macOS LaunchAgent 3개로 실행 중이다.
- 경기 시간대(수~일 17~23시 KST): 7분마다 (gameday)
- 그 외 시간(매일 0-16시 정각): 매시간 (hourly)
- 휴방일 안전망(월/화 매시간): (montue)
- **wrapper가 두 채널을 순차 fetch → `classify_videos.py --only-new` → `extract_winners.py --only-new`까지 한 번에 처리한다.** 한 채널 fetch가 실패해도 다음 채널은 시도, 파이프라인 뒷단은 계속 돈다.
- plist: `~/Library/LaunchAgents/com.invidam.lck-nospo.fetch-{gameday,hourly,montue}.plist`
- 자세한 표 / `launchctl` 사용은 README "권장 수집 스케줄" 참조

### VOD 매칭 작업 (향후)
- 매칭은 **반드시 backend에서만 실행**
- 검색 결과를 로깅할 때 영상 제목/썸네일 URL을 그대로 남기지 말 것 (디버그 모드 분리)
- 매칭 결과는 `{match_id, source, video_id, set_index, start_offset, end_offset}` 스키마로 저장

### 승팀 추출
- `vod_index/scripts/extract_winners.py` — 인터뷰 영상 제목 정규식 `^<승팀> <닉> 인터뷰 [ㅣ|] <팀A> vs <팀B>`에서 **첫 토큰을 승팀**으로 뽑는다. 첫 토큰이 매치업 두 팀 중 하나가 아니면 스킵 (방어). 구분자 문자 클래스는 LCK 공식의 ASCII `|`와 EWC 공식의 한글 `ㅣ`(U+3163)를 모두 잡는다 — 새 채널을 추가하다 winners 매칭 0건이면 여기에 구분자 추가 후보 1순위.
- `match_date`는 인터뷰 publish_date의 KST 자정. 경기일과 ±1일 차이 가능하므로 워커 쿼리에서 `match_date BETWEEN start - 1d AND start + 1d` 윈도우 JOIN으로 흡수.
- `--only-new`로 미처리 인터뷰만, 기본은 전체 재추출. PK 충돌은 `INSERT OR IGNORE`로 멱등.
- `wins_only=1` 구독자 알림은 전적으로 이 데이터에 의존. 인터뷰 없는 매치는 영영 안 보내지므로 사용자에게 명시 경고 필요 (이미 `/subscribe` 응답 + `/help` 양쪽 반영됨).

### 텔레그램 봇
- `notifier/scripts/telegram_bot.py` — `TELEGRAM_BOT_TOKEN`(환경변수 또는 `.env`) 필요
- 봇은 무상태. `data/vods/videos.sqlite`를 매 요청마다 새로 SELECT 한다. 데이터 적재 후 봇 재시작 불필요.
- 인라인 키보드는 **항상 5세트 × (하이라이트/풀영상) 고정**. 영상 없는 슬롯도 동일하게 노출 — BO3/BO5 구분과 시리즈 종료 여부 자체가 스포일러가 되지 않도록.
- **빈 슬롯과 영상 있는 슬롯이 시각적으로 구분되면 안 된다.** 현재 구현은 영상이 없는 슬롯도 placeholder URL(`PLACEHOLDER_URL` 상수)로 채워 URL button을 그린다. 그래야 같은 색·같은 모양으로 보임. (이전엔 callback button을 썼는데 텔레그램 클라이언트에서 색이 달라져서 영상 유무가 시각적으로 들켰음 — 무스포 위배라 placeholder URL 방식으로 전환.)
- 메시지 전송 시 항상 `disable_web_page_preview=True`.

#### 현재 구현된 명령어
- `/watch` — 팀 선택 키보드(`matches`에 등장한 팀들을 2열 그리드로). 누르면 `/watch <팀>` 자동 전송.
- `/watch <팀>` — 최근 10경기 (날짜+매치업만, 영상 유무 숨김)
- `/watch <MMDD> <팀>` 또는 `/watch <MMDD> <팀A> <팀B>` — 매치 찾기 → 5세트 그리드
- `/schedule` — 팀 선택 키보드 (다가오는 경기용)
- `/schedule <팀>` — 오늘 이후 다가오는 10경기
- `/subscribe` — 팀 선택 박스(matches에 등장한 모든 팀). 누르면 `/subscribe <팀>` 자동 전송.
- `/subscribe <팀>` — 대문자 정규화 후 matches 존재 검증 → `subscriptions`에 `INSERT OR IGNORE`.
- `/unsubscribe` — 현재 chat_id가 구독 중인 팀만 박스로. 0개면 안내 메시지.
- `/unsubscribe <팀>` — 해당 구독 DELETE.
- `/subscriptions` — 내 구독 목록 텍스트.
- `/help`, `/start` — 안내 + 하단 영구 키보드(`/watch` `/schedule` `/subscribe` `/subscriptions` `/unsubscribe` `/help`, 3행 구성)
- 날짜 입력은 MMDD 4자리. 연도는 KST 현재 연도 자동 (미래 날짜면 작년).
- 한 팀 검색 결과가 여러 매치면 매치 선택 키보드 → 클릭 시 같은 메시지를 5세트 그리드로 `edit_message_text` 교체.

> `/team` 명령은 더 이상 없다. `/watch <팀>`이 그 자리를 대신한다.

#### 구독/알림 (구현됨)

- **스키마 자동 생성**: 봇 부팅 시 `init_schema(conn)`가 `subscriptions(chat_id, team, wins_only, created_at)`, `notifications(chat_id, match_id, sent_at)` 두 테이블과 `idx_subscriptions_team` 인덱스를 `CREATE IF NOT EXISTS`로 멱등 생성. 마이그레이션 스크립트 별도 없음.
- **알림 워커**: `app.job_queue.run_repeating(notify_worker, interval=300, first=5)`. `python-telegram-bot[job-queue]` extras에 의존(apscheduler/tzlocal). JobQueue가 비활성이면 경고 로그 후 워커 스킵.
- **트리거**: `datetime(matches.start_at_kst, '+4 hours') <= datetime('now', '+9 hours')` AND `notifications.match_id IS NULL` AND `subscriptions.wins_only = 0`. 4시간 고정 지연이라 BO1/3/5 길이 추론 불가.
- **발송 순서**: send 먼저 → 성공 시 `notifications` INSERT. 실패 시 다음 tick에서 자동 재시도. 중복 발송 위험 < 미발송 위험.
- **rate limit**: chat_id당 0.5s sleep.
- **누락 복구**: 봇이 꺼져 있던 동안 트리거된 매치는 부팅 후 첫 tick(5초 뒤)에서 일괄 처리.
- **메시지 본문**: `/watch` 응답과 픽셀 단위 동일 (📅 날짜 + 매치업 + 5세트 그리드, `disable_web_page_preview=True`). "다시보기 가능", "결과 나왔어요" 같은 결과 암시 문구 절대 금지.
- **`wins_only` 컬럼**: 2차 워커 쿼리에서는 `wins_only=0`만 잡고, `wins_only=1`은 3차의 UNION ALL 분기에서 처리.

#### 승팀 추출 + wins_only 옵션 (구현됨)

- **스키마 자동 생성**: 봇 부팅 시 `init_schema(conn)`가 `match_winners` 테이블 + `idx_mw_lookup` 인덱스를 `CREATE IF NOT EXISTS`로 멱등 생성. extract_winners를 한 번도 안 돌렸어도 워커 JOIN이 빈 결과를 반환하며 정상 동작.
- **추출 스크립트**: `vod_index/scripts/extract_winners.py`. 정규식 `^<승팀> <닉> 인터뷰 | <팀A> vs <팀B>`로 첫 토큰을 승팀 추출. 첫 토큰이 매치업 두 팀에 없으면 스킵. PK `(match_date, team_a, team_b, source, source_id)` + `INSERT OR IGNORE`로 멱등. 현재 시드 23/23 정합 검증 완료.
- **`add_subscription` 시그니처**: `add_subscription(conn, chat_id, team, wins_only=False)`. 반환 `'inserted' | 'updated' | 'unchanged'`. 같은 팀이 이미 다른 옵션으로 구독 중이면 **UPDATE 덮어쓰기**.
- **`list_subscriptions`**: `list[tuple[str, int]]` (team, wins_only) 반환.
- **`/subscribe <팀> wins`**: 두 번째 인자는 `wins`(대소문자 무관)만 허용. 다른 값은 사용법 응답. wins_only=True 응답 본문에 ⚠ 무스포 경고 명시 ("알림이 오는 것 자체가 승리를 알려줍니다", 졌거나 인터뷰 미공개 시 영영 안 옴). `HELP_TEXT`에도 동일 경고 한 줄.
- **`/subscriptions` 출력**: wins_only=1 항목은 `• HLE (이겼을 때만)` 형태로 표시.
- **워커 쿼리 분기**: `find_due_notifications`에 UNION ALL로 `wins_only=1` 분기 추가. `match_winners` JOIN으로 승팀이 구독 팀일 때만 발송. 매치업 두 팀 순서 무관(`OR` 양방향). `match_date` ±1일 윈도우로 인터뷰 publish_date의 경기일 어긋남 흡수. seen set으로 두 분기 결과 dedup.
- **검증**: 메모리 DB로 일반/wins_only/승팀 미공개 매치 시나리오 모두 통과.

#### 현재 구현된 명령어 (추가)
- `/subscribe <팀> wins` — 위 항목 참조. (기존 `/subscribe <팀>`은 승패 모두 알림.)

#### 무스포 충돌 점검 (신기능 추가 시)
- 알림 유/무, 시점, 빈도가 결과/길이를 노출하는가?
- 매치 리스트에서 영상 유무, 풀/하이라이트 개수, 영상 길이를 사용자가 추론할 단서가 새는가?
- 영상이 호스팅되는 외부 페이지(치지직/유튜브)의 추천/댓글 차단은 별개 작업 (자체 redirect 페이지 마일스톤).

## 변경 시 체크리스트

- [ ] 사용자에게 노출되는 정보 중 결과를 유추할 수 있는 것이 없는가?
- [ ] 외부 API 메타데이터가 프론트엔드로 새지 않는가?
- [ ] CLI / 에러 메시지가 한국어인가?
- [ ] 새 파일명을 정자(`rounds-…`)로 저장했는가?
- [ ] 스크립트를 적절한 도메인의 `<domain>/scripts/` 아래에 두었는가?
- [ ] VOD 어댑터를 추가했다면 `videos` 공용 스키마를 확장하지 않고 매핑했는가?

## 알려진 이슈

- `.ics` 확장자이지만 내용은 CSV. iCalendar 파서로 열면 깨진다. `csv.DictReader`로 읽을 것.
