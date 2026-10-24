# 배포 현황 (STATUS)

> 최종 업데이트: 2026-10-24
> 이 문서는 배포 진행 상황·결정사항·운영 정보를 정리한 스냅샷이다.
> 실제 배포 방법은 `deploy/README.md`, 배포 자동화는 `deploy/deploy.sh`,
> 두 프로젝트 공통 규칙(정본)은 `~/PycharmProjects/vimos/shared/CONVENTIONS.md` 참고.

## 한 줄 요약

**2026-10-24 17:44 KST 배포 완료.** Worlds 일정 16건(10/24·10/25 각 8건)을 운영 DB에
적용했다. LCK 10팀 선택기, 일정·VOD 통합 수집 및 실행 이력 기록, UTC cron 보정도 반영했다.
원격 `/watch HLE` 응답에서 10/24 Worlds 경기가 첫 항목으로 표시되는지 확인했다.

---

## 운영 환경 (실측)

| 항목 | 값 |
|------|-----|
| 배포 방식 | Docker-in-LXC (Proxmox 노트북). stock-routine 과 같은 호스트, 다른 LXC |
| Proxmox 호스트 | `root@192.168.0.29` |
| LXC | CTID `202` (`ct-bots`, privileged + nesting=1,keyctl=1) |
| LXC IP | `10.10.10.102/24` (`vmbr0-test`, outbound only — 외부 포트포워딩 없음) |
| Docker | `29.7.1` (컨테이너 안 설치, `curl \| sh`) |
| 이미지 / 컨테이너 | `lck-nospo` / `lck-nospo-bot` |
| 소스 트리(LXC 내) | `/root/lck-nospo` |
| DB 볼륨 | `/opt/lck-nospo` → 컨테이너 `/data` (`videos.sqlite`) |
| 토큰 파일 | `/opt/lck-nospo/.env` → `--env-file` 주입 |
| run 옵션 | `--network host --security-opt apparmor=unconfined --restart unless-stopped` |
| 환경변수 | `MODE=bot`, `LCK_DB_PATH=/data/videos.sqlite` |
| 최초 부트스트랩 | matches 104 · videos 2957 · classifications 2957 · winners 141 |
| 현재 일정(2026-10-24 확인) | LCK 130 + RTE 14 + WORLDS 16 = 160건 |
| 서버 시간대 | UTC (`+00:00`), cron은 UTC로 작성 |
| 현재 이미지 ID | `sha256:7569892b1288dc4c044dc9a5edf42e11cc87eb181a66f10ffd0b6ee7c60dbc2f` |
| 코드 기준 | Git `18bcdb5` + 이번 작업의 미커밋 변경. 배포 버전은 위 이미지 ID로 식별 |

## 아키텍처 (배포 형태)

```
                      ┌──────────────────────────────┐
   호스트 cron ──────▶│ MODE=fetch 컨테이너 (--rm)    │  일정·치지직 VOD 수집
   (활동시간 7분)      │ schedule→fetch→classify→winners│  후 종료
                      └──────────────┬───────────────┘
                                     │ 같은 볼륨 공유
                      ┌──────────────▼───────────────┐
   텔레그램 사용자 ──▶│ MODE=bot 컨테이너 (상시)     │  polling, 매 요청 SELECT
                      │ lck-nospo-bot                │
                      └──────────────┬───────────────┘
                                     ▼
                       /opt/lck-nospo/videos.sqlite  (볼륨, 봇·수집 공유)
```

## 결정 사항 (이번 배포 준비)

- **단일 이미지 + `MODE` 분기**(`bot`/`fetch`)로 stock-routine 패턴을 그대로 이식.
- **DB 확장자는 lck 기존 `.sqlite` 유지** (stock 은 `.db`). 코드가 이미 통일돼 있어 건들지 않음.
- **VOD 수집 배치는 맥 LaunchAgent → 서버 cron 으로 이전**. 배포 후 맥의
  `com.invidam.lck-helper.fetch-*` LaunchAgent 3개는 중복 방지 위해 unload 권장.
- **토큰은 이미지에 굽지 않고** `/opt/lck-nospo/.env` + `--env-file` 로 주입.
- DB 경로를 `LCK_DB_PATH` env 로 override 가능하게 5개 스크립트 최소 수정
  (봇·fetch·classify·winners·import). 미설정 시 기존 로컬 경로 그대로.

## 배포 이력

**2026-10-24 Worlds 일정·자동화·팀 선택기 배포 (완료):**
- [x] 구독형 iCalendar → `matches` 어댑터 및 회귀 테스트 추가
- [x] 기본 드라이런, Leaguepedia CSV 비교, 기존 `match_id` 보존 연결 구현
- [x] 실제 피드 임시 DB 2회 실행으로 신규→변경 없음 멱등성 확인
- [x] 기존 LCK/RTE CSV를 이전 importer·새 비교기로 읽어 날짜·매치업·시각 일치 검증
- [x] 원격 DB 복제본: 기존 144건 보존, Worlds 신규 16건, 재실행 신규 0건
- [x] 10/24 Worlds 8경기 모두 기존 VOD 조회 함수와 연결됨 확인
- [x] 17:39 KST 운영 DB에 일정 16건 적용. 2차 통합 배치에서 변경 없음 16건·충돌 0
- [x] `MODE=fetch`에 일정 동기화 통합. HTTP/파싱 실패도 DB 실행 이력에 기록. 별도 일정 cron 없음
- [x] KST 01~12시·17~23시에는 매일 7분마다, 나머지 시간은 매시간. UTC 시각으로 cron 교체
- [x] `/watch`·`/schedule`·`/subscribe` 목록 LCK 10팀. 구독 해제 목록도 구독한 LCK 팀만 표시
- [x] 시작 알림 5분 재스캔, 시각·대진 변경 시 예약 교체
- [x] 로컬 테스트 35개 통과. 배포 이미지에서도 35개 실행(34 통과, 로컬 파일 DB smoke 1개 제외)
- [x] 실제 원격 `cmd_watch` 호출을 응답 mock으로 검증: 첫 항목 `1024 [WORLDS] HLE vs TES`
- [x] 봇 실행·polling 헬스체크 및 `PRAGMA quick_check=ok`
- [x] README·AGENTS·일정 설계·배포 가이드·본 문서 갱신, 공통 `deployments.json` 기록

Worlds Leaguepedia CSV 원본은 미확보라 동일 대회 두 소스 간 실데이터 동등성 비교는 미검증이다.
원본 피드는 일 1회 생성되며 개인 Google Calendar의 갱신 주기는 봇과 무관하다.
10/25 04:00 HLE 일정은 이미 저장되어 있다. 경기 후 치지직 게시·수집·분류가 끝나면 연결된다.
이번 검증은 미래 경기 VOD의 게시 시각까지 보장하는 검증은 아니다.

백업(LXC 내):
- DB: `/opt/lck-nospo/backups/pre-worlds-20261024T083521Z.sqlite` (`quick_check=ok`)
- 기존 소스: `/opt/lck-nospo/backups/source-pre-worlds-20261024.tar.gz`
- 기존 cron: `/opt/lck-nospo/backups/crontab-pre-worlds-20261024.txt`
- 기존 이미지: `lck-nospo:pre-worlds-20261024`

**2026-08-02 최초 배포 (완료):**
- [x] LXC 202(`ct-bots`) 생성 — privileged, nesting/keyctl, `10.10.10.102`, apparmor unconfined
- [x] 컨테이너 안 Docker 29.7.1 설치
- [x] `./deploy/deploy.sh` — 이미지 빌드 → 봇 기동 → 헬스체크 통과
- [x] 부트스트랩 — 일정 import(104) + VOD 초기 수집(2957) 완료
- [x] `crontab deploy/crontab` 등록 (cron 데몬 active). `MODE=fetch` 배치 수동 실행으로 증분 동작 확인
- [x] STATUS 갱신

## 남은 작업

- [ ] **맥 LaunchAgent(`com.invidam.lck-helper.fetch-*`) unload** — 서버 cron 과 중복 수집 방지.
      `launchctl bootout gui/$(id -u)/com.invidam.lck-helper.fetch-{gameday,hourly,montue}`
- [ ] 호스트 재부팅 영속성 검증 — `onboot:1` 자동 기동 + `--restart unless-stopped` 확인
      (docker-in-lxc.md §8)

## 롤백 / 중지

이번 배포를 되돌릴 때는 먼저 배치를 멈추고 백업 cron과 `lck-nospo:pre-worlds-20261024` 이미지를
사용해 같은 볼륨·run 옵션으로 봇을 기동한다. DB 전체 복구는 백업 이후 알림·구독·VOD 변경도
되돌리므로 필요 시 현재 DB를 먼저 추가 백업하고 수행한다.

```bash
# 봇 중지
ssh root@192.168.0.29 "pct exec 202 -- docker stop lck-nospo-bot"
# DB 는 볼륨(/opt/lck-nospo/videos.sqlite)에 남아 있으므로 재기동하면 그대로 복구
```
