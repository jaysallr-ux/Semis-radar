# guru_x: Macro Guru X 원문 수집기

Macro Guru Stance Watch(GPT 예약 알람)가 X 원문을 직접 못 여는 문제를 해결하려고 만든 보조 수집기.
semis-radar 와 같은 역할 분담을 따른다.

- X 공식 API = 사실 (원문, 작성자 user_id, 시각, URL)
- Python = 수집, 중복 제거, 7일 보관
- GPT = `guru_x/data/feed.json` 을 읽고 판정

## 데이터 소스 (자동 선택)

| 조건 | provider | 탐색 (새 글 찾기) | 검증 (원문·작성자) |
|---|---|---|---|
| `X_BEARER_TOKEN` 있음 | `x_api` | X 공식 API 타임라인 | X 공식 API (`verification: x_api`) |
| 없음 | `free` | 공개 X 리스트 임베드 (`x_list_id`) + 수동 ID (`data/manual_ids.json`) + `XAI_API_KEY` 있으면 Grok x_search | X syndication (`syndication`), 실패 시 X oEmbed (`oembed`) |

free 경로 원칙
- Grok 이 쓴 문장은 저장하지 않는다. 출처 URL 만 뽑는다
- 원문, 작성자, 시각은 X 서버가 돌려준 값만 쓴다. 작성자가 추적 대상 핸들과 다르면 버린다
- 남에게 단 답글은 버리고, 본인 스레드 이어쓰기는 `is_self_reply: true` 인 개별 post 로 넣는다
- `discovery_complete` 는 리스트 임베드의 가장 오래된 글이 지난 성공 수집 시점보다 이전일 때만 true. false 면 피드에 없다고 그 사람이 글을 안 쓴 게 아니다
- 2026-10-22 확인: 프로필 임베드는 Actions·가정용 IP 모두 429. 공개 리스트 임베드는 Actions 에서 정상(78개). 리스트를 비공개로 바꾸면 비어서 나온다

`discovery_stats` 에 계정별 프로필 임베드 상태, Grok 상태, 찾은 URL 수, 검증 결과별 개수가 찍힌다.
`oembed_selftest` 는 매 실행마다 알려진 게시물 하나로 syndication 과 oEmbed 가 살아있는지 확인한 결과다.

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

free 경로 post 추가 필드
- `quoted_post`: 인용한 글의 id, author, created_at, url, text, available, has_media (인용글 원문은 X syndication 이 돌려준 값)
- `links`: 본문의 t.co 링크를 펼친 실제 주소
- `has_media`: 이미지/영상 첨부 여부. 텍스트만으로 뜻을 알 수 없을 수 있다는 표시
- `discovered_via`: list_embed / manual_browser / grok / profile_embed
- `rev`: 레코드 형식 버전. 낮은 버전 레코드는 다음 실행에서 무료로 재검증된다

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

무료로 갈 때
1. 키 없이도 free 경로가 돈다. 다만 프로필 임베드가 막혀 있으면 탐색이 안 되므로 Grok 을 붙이는 게 현실적
2. console.x.ai 가입, 무료 크레딧과 데이터 공유 크레딧 적용 여부 확인, 지출 한도 설정
3. API 키를 `XAI_API_KEY` 이름으로 등록 (`X_BEARER_TOKEN` 은 비워둘 것. 있으면 X API 가 우선)
3. Actions 탭에서 `guru-x-feed` 를 Run workflow 로 한 번 수동 실행해서 `feed.json` 생성 확인

## 계정 추가/수정

`accounts.json` 만 고치면 된다. 핸들이 틀리면 `errors` 에 "계정 조회 실패"로 찍힌다.
