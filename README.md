# semis-radar: 반도체 레짐·순환매 레이더

역할 분담
- 데이터 소스(naver/pykrx/KIS) = 사실
- Python = 계산
- 백테스트 = 임계값 결정
- GPT = latest.json 읽고 상태 변화만 알림 (숫자 계산 금지)
- 솬이 = 주도 파트 안에서 눌림 진입 결정

## 파일 구조

| 파일 | 역할 |
|---|---|
| data_provider.py | 데이터 소스 어댑터. 소스 교체는 여기만 고치면 됨 |
| indicators.py | 종목별 지표 + 추세 ON/OFF 상태머신 |
| regime.py | 폭, peer RS, 주도 파트/하닉, 체인, 후반 경고 판정 |
| run_daily.py | 매일 실행 → output/latest.json |
| backtest.py | 실제 사이클 구간 + 하닉 확인점 기준 백테스트 |
| universe.json | 28종목, 공정 분류, 고객사 태그 |
| params.json | 모든 임계값 |
| memory_price.json | 메모리 가격 모멘텀 수동 입력 (주 1회) |
| .github/workflows/daily.yml | 평일 16:37 KST 자동 실행 |

## 판정 규칙 요약

- 추세 ON: 종가 > 20MA, 20MA 5일 상승, 20일 수익률 > 0
- 추세 OFF: 종가 < 20MA 2일 연속, 또는 20MA 하락 + 20일 수익률 < 0
- 신규전환: OFF 10거래일 이상 유지 후 ON 만 인정
- RS 기준: peer 평균 = 그날 유효한 종목들의 20일 수익률 단순 평균. JSON 필드는 `rs20_vs_peers`, `market.peer_avg_ret20_pct`
  - 백테스트 이후 수익률은 별개의 일별 동일가중 누적지수 `ew_index` 기준
- 파트 커버리지: 그날 데이터가 있어야 정상인 종목 대비 유효 종목이 2개 이상이고 66% 이상인 파트만 판정 참여
  - 상장 전이거나 valid_from 이전 종목은 분모에서 빠짐
  - 수신 실패 종목은 분모에 남아서 결측으로 잡힘
  - 제외 파트는 `latest.json.parts_excluded` 에 기록
- valid_from: 해당 날짜 이전 데이터는 지표 계산 전에 잘라서 과거 추세 상태가 넘어오지 않게 함
- 초입후보: 최근 10거래일 신규전환 폭 + 참여 파트 수 + 거래량 1.3배 종목 비율
- KOSPI 대비 RS: 참고지표 전용. 어떤 상태 발동/해제 조건에도 사용하지 않음
- 사이클확인: 추세 ON 비율 + 과반 ON 파트 수 + 하닉 추세 ON
- 하닉 본체 주도: 하닉 peer RS 발동 +10%p / 해제 +5%p 미만
- 주도 파트: 파트 peer RS 중앙값 발동 +8%p / 해제 +4%p 미만, 과반 ON, 과반 거래량 증가
  - 다른 파트가 진입조건을 만족하면서 현 리더보다 +5%p 이상 강한 상태가 2거래일 연속이면 교체
- 체인: 태그된 고객사 체인별 ON 비율 발동 60% / 해제 40%
- 후반 경고: 후행파트 확산, 주도 RS 고점하락, 대장 신고가 실패, 하닉 추세 OFF, 메모리가격 둔화 중 2개 주의 / 3개 경고 / 4개 이상 강한 경고

## 1차 실데이터 튜닝값

2015~2026 네이버 수정주가 일봉으로 4개 역사적 사이클을 검증한 뒤 다음 값을 1차 운영값으로 채택했다.

- `new_turn_window = 10`
- `early_enter_breadth = 0.40`
- `confirm_enter_on_share = 0.60`
- `confirm_min_parts = 5`
- `confirm_exit_on_share = 0.45`

4개 사이클밖에 없고 현재 생존 종목 기준이라는 한계가 있으므로 이 값은 영구 고정값이 아니다. 신규 사이클·실전 데이터가 쌓이면 재검증한다.

## 알림 규칙

- 비교 기준은 `latest.json.last_ok_state` = 마지막 정상 판정 상태. 실행이 하루 빠져도 그 사이 변화를 놓치지 않음
- 첫 정상 실행: 기준선만 만들고 알림 없음
- 새 일봉 없음(휴장일·재실행): 알림 없음
- 데이터 오류: 직전 오류와 내용이 달라졌을 때만 재알림
- 오류 → 정상 복귀: `[데이터 정상화]` 1회
- 하닉·삼전 첫 일봉이 요청 시작일보다 20일 이상 늦으면 소스 기간 잘림으로 보고 판정 보류
- **장중 오염 방지:** 평일 15:40 KST 전 `naver/pykrx/KIS` 실행은 판정·파일 저장 자체를 스킵한다. 종가 기반 레이더에 장중 provisional bar가 들어오는 것을 막기 위한 방어다.
  - 정말 장중 진단이 필요할 때만 `--allow-intraday` 사용
  - 생산 스케줄에서는 `--allow-intraday` 금지

## GitHub 운영

1. 공개 저장소 사용
2. Actions workflow에 `contents: write` 권한 부여
3. 평일 16:37 KST `semis-radar-daily` 실행
4. `output/latest.json`을 커밋
5. GPT는 평일 17:10 이후 raw JSON만 읽음

## 검증 순서

1. **데이터 원천 검증**: `latest.json.stocks` 의 하닉·삼전·주성·심텍·리노 종가를 별도 시세원과 대조
2. **계산 검증**: 표본 종목의 `ret20_pct`, `ma20`, `dist_52w_high_pct`를 원시 일봉으로 재계산해 대조
3. **전달 검증**: raw URL을 GPT가 읽어 JSON 원본과 숫자를 정확히 동일하게 읽는지 확인
4. **백테스트**: `python backtest.py --provider naver --grid`
   - 실제 사이클 구간 기준 탐지와 하닉 +30% 확인점 기준 비교를 분리
   - 사이클 진행 중 재진입은 false positive로 세지 않음
   - 실제 사이클 시작 전 60거래일까지 조기탐지 허용구간
5. 위 검증 통과 뒤 GPT 예약 작업 활성화

## GPT 예약 작업 프롬프트 템플릿

평일 17:10 KST 실행:

    아래 URL의 JSON을 읽어라: (raw URL)
    규칙:
    1. generated_at 날짜가 오늘이 아니면 "레이더 파이프라인 멈춤: 마지막 실행 (generated_at)" 알림.
    2. data_ok 가 false 이고 alert.changed 가 true 면 alert.message 를 그대로 알림.
    3. alert.changed 가 true 면 alert.message 를 그대로 전달하고,
       state.phase_kr, state.leader, state.active_chains, state.late_level,
       market.breadth_early, market.on_share 값을 JSON에 적힌 숫자 그대로 덧붙여라.
    4. alert.changed 가 false 면 아무것도 보내지 마라.
    5. 숫자를 직접 계산하거나 추정하지 마라. JSON에 없는 값은 "없음"이라고 써라.

## 고객사 태그

`universe.json` 각 종목 `chain` 을 사업보고서/IR의 주요 매출처 근거로 `하닉 / 삼전 / 공통 / 기타AI` 중 하나로 수정.
`미확인`은 체인 집계에서 제외한다. 기억으로 추정하지 않는다.

## 데이터 소스 교체

`data_provider.py` 에 provider 클래스 추가 → `get_provider()` 등록 → `daily.yml` 의 `--provider` 값만 변경.
계산 코드는 건드리지 않는다. API 키는 GitHub Secrets 로만 주입한다.

## 한계

- Naver fchart는 비공식 엔드포인트다. 현재 GitHub runner에서 실데이터 수신을 검증했지만 구조 변경 시 깨질 수 있음
- Naver XML은 EUC-KR이므로 명시적으로 디코딩 후 파싱함
- 거래대금 대신 거래량 배수를 사용
- KOSPI 수신 실패는 참고지표만 사라질 뿐 레짐 판정에는 영향 없음
- 백테스트는 현재 살아남은 종목 중심이라 생존편향이 있음
- 4개 역사적 사이클만으로 임계값을 최적화하면 과적합 위험이 있으므로 plateau/중간값을 우선하고 실전에서 재검증한다


## 백테스트 v3 (진입 품질 분리)

평가를 세 층으로 나눈다: 사이클 탐지 / 진입 품질 / 랜덤 기준선.

- **사이클 라벨**: 동일가중 지수에서 자동 정의(지그재그 25% 전환, 저점→고점 +50% 이상, 120거래일 이상)가 메인. 사람이 정한 수동 구간은 비교용
- **진입 품질(고정, 튜닝 금지)**: 신호 후 60거래일 MAE가 -10% 이내 AND 120거래일 수익률이 랜덤 기준선 중앙값 이상이면 진입 성공
- **신호 라벨**: 헛방 / 너무 이름 / 선행 진입 성공 / 진입 성공 / 진입 실패 / 후반 재진입(사이클 진행률 2/3 이후) / 판정대기(기간부족)
- **랜덤 비교**: 같은 개수의 랜덤 날짜 5000회와 비교해서 사이클 안 비율, 120일 평균 수익률이 몇 백분위인지
- **하나 빼고 검증(--grid)**: 사이클 하나 빼고 나머지로 파라미터 고른 뒤, 뺀 사이클로 테스트. 사이클마다 고른 값이 같으면 안정적

결과 파일: backtest/summary.json, signals_auto.csv, signals_manual.csv, grid.csv, loco.csv

## 상장일(list_date) 가드

- 모든 종목은 기본적으로 '요청 시작일부터 데이터가 있어야 정상'
- 진짜 늦게 상장한 종목만 universe.json 에 list_date 를 KRX 상장일로 기입
- 첫 일봉이 기대 시작일보다 20일 넘게 늦으면 백테스트 중단 (소스가 과거를 자른 걸 상장 전으로 착각하지 않게)
- 후보 확인: Actions → semis-radar-backtest → mode=suggest. 출력된 날짜를 KRX 상장일과 대조해서 같을 때만 기입

## 늦은 마감일

수능일처럼 장 마감이 16:30인 날은 params.json 의 late_close_dates 에 날짜를 넣으면 16:40 이전 실행이 차단됨
