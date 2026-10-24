# 일정 수집

`import_schedule.py`는 Leaguepedia CSV 수동 적재, `sync_ical_schedule.py`는 공개 iCalendar
피드 자동 수집을 담당한다. 외부 입력은 `ScheduleEvent`로 정규화한 뒤 비교·DB 저장 로직을 거친다.
향후 PandaScore REST API를 붙일 때도 같은 정규화 데이터와 저장·비교 로직을 사용한다.

## 현재 운영 흐름

기존 서버 `MODE=fetch` 또는 맥 `bin/run_fetch_pipeline.sh` 실행 시 Worlds 일정을 먼저 동기화한다.
일정 전용 cron은 두지 않는다. 수집 주기는 VOD 배치와 같고, 원본 피드는 일 1회 갱신된다.
UTC 서버에서 KST 01~12시·17~23시는 7분마다, 나머지는 매시간 실행한다.
`MODE=schedule`은 수동 점검·재실행용이다.

- 대상: `WORLDS`, 2026년, KST 10/24 이후 확정 대진.
- URL: `https://www.tompka.io/lol-events/cal/league-of-legends-world-championship.ical`
- DB: `LCK_DB_PATH`, 기본 `data/vods/videos.sqlite`.
- 범위 설정: `LCK_SCHEDULE_SEASON`, `LCK_SCHEDULE_FROM_DATE`.
- 식별: 외부 UID를 `schedule_events`로 매핑해 `match_id`를 유지한다.
- 기존 데이터 연결 후보가 여러 개면 전체 변경을 롤백한다. 기존 행의 자동 삭제는 하지 않는다.
- `TBD` 대진은 다음 실행까지 제외한다. 원본 피드의 경기 취소·삭제는 자동 삭제로 반영하지 않는다.
- 결과·종료 시각·길이는 DB와 로그에 저장하지 않는다. 팀 약어는 VOD 표기와 맞춘다.

## 실행 이력과 장애 확인

`--apply` 실행은 HTTP 요청 전에 `schedule_sync_runs`에 `running`을 기록한다. 성공하면 건수와
`success`, 조회·파싱 실패는 `failed`, 비교 불일치·중복 연결 충돌은 `rejected`로 종료한다.
프로세스 강제 종료 시 남은 `running`도 장애 징후다. 콘솔 로그는 stdout으로 출력하며 서버의
기존 `/var/log/lck-nospo-fetch.log`에 모인다. 드라이런은 운영 DB의 복제본을 메모리에서 사용한다.

```bash
.venv/bin/python schedule/scripts/sync_ical_schedule.py --season 2026 --from-date 2026-10-24
python3 -m unittest discover -s schedule/tests -v
python3 -m unittest discover -s notifier/tests -v
```

## 전환 검증과 한계

기존 LCK/RTE CSV를 이전 importer와 새 비교기로 각각 읽어 날짜·순서 무관 매치업·시각이 같은지
검증한다. Worlds Leaguepedia CSV가 없으므로 동일 Worlds 대회의 두 원본 간 동등성은 미검증이다.
CSV를 확보하면 `--compare-csv`로 비교할 수 있다. 빈 비교 범위는 실패 처리한다.

운영 DB 복제본에서는 기존 144건 보존, Worlds 16건 추가, 재실행 신규 0건과 10/24 VOD 조회 연결
8경기를 확인했다. 시작 알림은 5분마다 일정을 재스캔하고 변경된 예약을 교체한다.
`/watch`·`/schedule`·`/subscribe` 선택기는 LCK 10팀만 표시한다. `/unsubscribe`는 구독한 LCK 팀만
표시하며 다른 팀의 기존 구독을 자동 삭제하지 않는다. 팀 약어 직접 입력은 계속 지원한다.

피드 갱신 지연이나 실제 시작 시각 보정이 알림 시각에 영향을 줄 수 있다. 더 빠른 갱신이 필요하면
PandaScore API 토큰을 런타임에 주입하는 별도 입력 어댑터를 추가한다.
