# guru_x: Macro Guru X 원문 수집기

Macro Guru Stance Watch(GPT 예약 알람)가 X 원문을 직접 못 여는 문제를 해결하려고 만든 보조 수집기.
semis-radar 와 같은 역할 분담을 따른다.

- X 공식 API = 사실 (원문, 작성자 user_id, 시각, URL)
- Python = 수집, 중복 제거, 7일 보관
- GPT = `guru_x/data/feed.json` 을 읽고 판정

## 데이터 소스 (둘 중 하나, 자동 선택)

| 우선순위 | 시크릿 | 방식 | posts[].verification |
|---|---|---|---|
| 1 | `X_BEARER_TOKEN` | X 공식 API로 원문·작성자 user_id 직접 수신 | `x_api` |
| 2 | `XAI_API_KEY` | Grok x_search 로 게시물 URL만 탐색 → X 공개 oEmbed 로 원문·작성자 확인 | `oembed` |

Grok 경로에서 Grok 이 쓴 문장은 저장하지 않는다. 시스템이 붙인 출처 URL만 뽑아서 oEmbed 로 원문을 받고,
작성자가 추적 대상 핸들과 다르면 버린다. 작성 시각은 게시물 ID(snowflake)로 계산한다.
한계: Grok 탐색이라 일부 글을 놓칠 수 있고, 긴 글은 oEmbed 에서 잘릴 수 있다(`possibly_truncated`).
`discovery_stats` 에 찾은 URL 수, 검증 성공, 작성자 불일치, oEmbed 실패 수가 찍힌다.
`oembed_selftest` 는 매 실행마다 알려진 게시물 하나로 oEmbed 가 살아있는지 확인한 결과다.

## 실행 시각

KST 10:15, 21:15에 실행하고, 실패하거나 지연되면 20분 뒤 한 번 더 시도한다.
GPT 알람(11:00, 22:00)보다 45분 먼저 끝나도록 잡았다.

## 출력: `guru_x/data/feed.json`

| 필드 | 뜻 |
|---|---|
| generated_at | 수집 시각 (KST) |
| data_ok | 모든 계정 수집 성공이면 true |
| errors | 계정별 실패 사유 |
| accounts[] | 계정별 last_fetch_ok, last_success_at, new_originals_this_run |
| posts[] | 최근 7일 원글. text는 원문 그대로. X API 경로는 280자 넘는 글도 전문, thread는 본인이 이어 쓴 답글만. Grok 경로는 스레드 글도 개별 post로 들어감 |
| cost_this_run | 이번 실행 과금 추정 |

리트윗과 남에게 단 답글은 수집하지 않는다. 본인 스레드 이어쓰기만 원글에 붙인다.

## 비용

X API (2026-09 기준 pay-per-use): 게시물 1개 반환당 $0.005, 사용자 조회 1명당 $0.01.
Grok 경로 (2026-10 기준): x_search 게시물 1000개당 $5 + 토큰(grok-4.3 입력 $1.25/M, 출력 $2.5/M).
정가로는 Grok 이 X API 보다 싸지 않다. xAI 무료 크레딧(가입 크레딧, 데이터 공유 월 크레딧)이 적용되면 사실상 무료.
모델은 `accounts.json` 의 `grok.model` 에서 바꿀 수 있다.
같은 게시물은 UTC 하루에 한 번만 과금된다.

- 첫 실행: 계정당 최근 7일 원글 최대 20개 + 스레드 → 약 $1 내외
- 평상시: 하루 약 $0.2~0.6 예상 → 월 $6~18
- 실행당 과금 한도: `accounts.json` 의 `max_posts_billed_per_run` (기본 300개 = $1.5)
- `guru_x/data/runs.csv` 에 실행별 과금 추정치가 쌓인다

X Developer Console 에서 월 지출 한도를 꼭 따로 걸어둘 것.

## 설정

X API 로 갈 때
1. X Developer Console 에서 앱 생성, pay-per-use 크레딧 충전, 월 지출 한도 설정
2. 앱의 Bearer Token 을 이 레포 Settings → Secrets and variables → Actions 에 `X_BEARER_TOKEN` 이름으로 등록

Grok 으로 갈 때
1. console.x.ai 가입, 무료 크레딧과 데이터 공유 크레딧 적용 여부 확인, 지출 한도 설정
2. API 키를 `XAI_API_KEY` 이름으로 등록 (`X_BEARER_TOKEN` 은 비워둘 것. 둘 다 있으면 X API 가 우선)
3. Actions 탭에서 `guru-x-feed` 를 Run workflow 로 한 번 수동 실행해서 `feed.json` 생성 확인

## 계정 추가/수정

`accounts.json` 만 고치면 된다. 핸들이 틀리면 `errors` 에 "계정 조회 실패"로 찍힌다.
