# 배포 현황 (STATUS)

> 최종 업데이트: 2026-08-02
> 이 문서는 배포 진행 상황·결정사항·운영 정보를 정리한 스냅샷이다.
> 실제 배포 방법은 `deploy/README.md`, 배포 자동화는 `deploy/deploy.sh`,
> 두 프로젝트 공통 규칙(정본)은 `~/PycharmProjects/vimos/shared/CONVENTIONS.md` 참고.

## 한 줄 요약

**배포 완료 · 운영 중.** `ct-bots`(CTID 202) LXC 에서 텔레그램 봇이 상시 polling 하고
(Docker-in-LXC), cron 이 경기 시간대마다 VOD 를 증분 수집한다. 배포는 `deploy/deploy.sh`
한 줄로 자동화되어 있다. 최초 부트스트랩으로 일정 104건 · VOD 2957건이 적재됐다.

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

## 아키텍처 (배포 형태)

```
                      ┌──────────────────────────────┐
   호스트 cron ──────▶│ MODE=fetch 컨테이너 (--rm)    │  치지직 신규 VOD 수집
   (경기시간 7분)      │ fetch→classify→winners       │  후 종료
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

```bash
# 봇 중지
ssh root@192.168.0.29 "pct exec 202 -- docker stop lck-nospo-bot"
# DB 는 볼륨(/opt/lck-nospo/videos.sqlite)에 남아 있으므로 재기동하면 그대로 복구
```
