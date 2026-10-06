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
| regime.py | 폭, 바스켓 RS, 주도 파트/하닉, 체인, 후반 경고 판정 |
| run_daily.py | 매일 실행 → output/latest.json |
| backtest.py | 과거 기준점 대비 탐지 검증 + 임계값 그리드 |
| universe.json | 28종목, 공정 분류, 고객사 태그 |
| params.json | 모든 임계값 |
| memory_price.json | 메모리 가격 모멘텀 수동 입력 (주 1회) |
| .github/workflows/daily.yml | 평일 16:37 KST 자동 실행 |

## 판정 규칙 요약

- 추세 ON: 종가 > 20MA, 20MA 5일 상승, 20일 수익률 > 0
- 추세 OFF: 종가 < 20MA 2일 연속, 또는 20MA 하락 + 20일 수익률 < 0
- 신규전환: OFF 10거래일 이상 유지 후 ON 만 인정
- RS 기준: 동료 평균(peer avg) = 그날 유효한 종목들의 20일 수익률 단순 평균. JSON 필드는 rs20_vs_peers, market.peer_avg_ret20_pct
  - 백테스트 이후 수익률은 별개인 일별 동일가중 누적지수(ew_index) 기준
- 파트 커버리지: 그날 데이터가 있어야 정상인 종목 대비 유효 종목이 2개 이상 그리고 66% 이상인 파트만 판정에 참여
  - 상장 전이거나 valid_from 이전 종목은 분모에서 빠짐 (과거 구간 불이익 없음)
  - 수신 실패 종목은 분모에 남아서 결측으로 잡힘 → 한 파트 데이터만 깨져도 가짜 주도 파트 방지
  - 제외된 파트는 latest.json 의 parts_excluded 에 표시
- valid_from: 그 날짜 이전 데이터는 지표 계산 전에 잘라냄. 이전 구간의 추세 상태가 넘어오지 않음
- 초입후보: 최근 10거래일 신규전환 비율, 참여 파트 수, 거래량 1.3배 종목 비율
- KOSPI 대비 RS: 참고지표로만 기록. 수신 실패해도 판정이 바뀌지 않게 어떤 조건에도 안 씀
- 사이클확인: 추세 ON 비율, 과반 ON 파트 수, 하닉 추세 ON
- 하닉 본체 주도: 하닉 RS(바스켓 대비) 발동 +10%p / 해제 +5%p 미만
- 주도 파트: 파트 RS 중앙값 발동 +8%p / 해제 +4%p 미만, 과반 ON, 과반 거래량 증가
  - 교체: 진입조건 만족하는 다른 파트가 현 리더보다 +5%p 이상 강한 상태가 2거래일 연속이면 교체
- 체인: 태그된 체인별 ON 비율 발동 60% / 해제 40%
- 후반 경고: 후행파트 확산, 주도 RS 고점하락, 대장 신고가 실패, 하닉 추세 OFF, 메모리가격 둔화 중 2개 주의 / 3개 경고 / 4개 이상 강한 경고

## 알림 규칙

- 비교 기준은 '마지막으로 정상 판정된 상태'(latest.json 의 last_ok_state). 실행이 하루 빠져도 변화를 놓치지 않음
- 첫 정상 실행: 기준선만 만들고 알림 없음
- 새 일봉 없음(휴장일·재실행): 알림 없음
- 데이터 오류: 오류 내용이 직전과 달라졌을 때만 알림 (같은 오류 지속이면 침묵, 결측 종목이 바뀌면 다시 알림)
- 오류 → 정상 복귀: [데이터 정상화] 1회 알림
- 데이터 방어: 하닉·삼전 첫 일봉이 요청 시작일보다 20일 이상 늦으면 소스가 기간을 잘랐다고 보고 판정 보류

## 셋업 순서

1. 깃허브에 **공개(public)** 저장소 만들고 이 폴더 내용 전부 업로드 (.github 폴더 포함)
2. 저장소 Settings → Actions → General → Workflow permissions → **Read and write** 선택
3. Actions 탭 → semis-radar-daily → **Run workflow** 로 수동 1회 실행
4. 실행 로그에서 오류 없는지, output/latest.json 커밋됐는지 확인

## 검증 순서 (이거 다 통과하기 전엔 GPT 알람 켜지 말 것)

1. **데이터 원천 검증**: latest.json 의 stocks 에서 하닉·심텍·주성 등 3~5종목 close 를 HTS 수정주가 종가와 대조. 1원이라도 다르면 원인 확인
2. **계산 검증**: 같은 종목 ret20_pct, ma20, dist_52w_high_pct 를 HTS 차트로 손계산 대조
3. **전달 검증**: raw URL (https://raw.githubusercontent.com/계정/저장소/main/output/latest.json) 을 GPT에 읽혀서 하닉 종가, 심텍 rs20_vs_peers, market.breadth_early 를 JSON 원본과 **정확히 같은 숫자**로 읽는지 확인
4. **백테스트**: 로컬에서 python backtest.py --provider naver --grid 실행
   - 시작 부분에 종목별 첫 일봉 날짜가 찍힘. 하닉·삼전이 2015년 초가 아니면 자동 중단
   - 기준점별 상태: 탐지 / 창 밖 선행(너무 이름) / 미탐지. 창이 열리기 전부터 켜져 있던 신호는 탐지로 안 셈
   - 탐지 수, 창 밖 선행 수, 헛방 수를 같이 보고 params.json 조정
5. 위 4개 통과 후에만 GPT 예약 작업 활성화

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

## 고객사 태그 채우기

universe.json 각 종목 chain 을 사업보고서 주요 매출처 기준으로 하닉 / 삼전 / 공통 / 기타AI 중 하나로 수정.
미확인 상태 종목은 체인 집계에서 빠진다. 기억으로 찍지 말 것.

## 데이터 소스 교체 (KIS 등)

data_provider.py 에 클래스 추가 → get_provider() 에 등록 → daily.yml 의 --provider 값만 바꾸면 끝.
계산 코드는 안 건드림. API 키는 반드시 GitHub Secrets 로만.

## 한계

- naver 소스는 비공식 엔드포인트. 구조 바뀌면 깨질 수 있음 → 실패 시 data_ok=false 로 판정 보류
- 거래대금이 아니라 거래량 배수 사용
- KOSPI 지수 수신 실패 시 KOSPI 보조 RS 조건은 자동으로 건너뜀
- 백테스트는 현재 생존 종목만 써서 생존편향 있음. 목적은 폭 신호가 사이클 초입 근처에서 작동했는지 확인하는 것
- 백테스트 기준점은 월봉 분석에서 하닉이 +30% 넘은 시점이라 '사이클 시작'과 정확히 같지는 않음
