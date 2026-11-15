# Claude API 판 (시험 중)

같은 아침 브리핑을 Claude 클라우드 예약 작업 대신 **Claude API**로 만드는 판입니다. 시험하는 동안은 `api-runs` 브랜치에만 쓰고, 폰이 가져가는 `main`의 `briefing.json`은 건드리지 않습니다.

## 파일

| 파일 | 하는 일 |
|---|---|
| `briefing_api.py` | 조사 → 정리 → (필요하면) 다시 쓰기 → 쓰기 |
| `rules.md` | 지시문. 지금 예약 작업의 지시문을 옮기고, 날짜 계산 · git 단계는 프로그램 몫으로 뺌 |
| `test_briefing_api.py` | 가짜 응답으로 흐름 · 검사를 보는 시험(API 안 부름) |
| `test_sdk_flow.py` | 진짜 SDK를 거치되 가짜 서버가 받는 시험 — 실제로 나가는 요청 모양을 봄(API 안 부름 · SDK 없으면 건너뜀 · Actions에선 늘 돎) |
| `requirements.txt` | `anthropic` SDK 버전 고정 |
| `../.github/workflows/briefing-api.yml` | GitHub Actions 예약 실행 |

## 순서

1. **조사** — `claude-opus-5-5`가 웹 검색 · 웹 가져오기로 밤사이 시장과 2주 일정을 찾아 JSON 초안을 냅니다. 서버 쪽 도구 반복이 멈추면(`pause_turn`) 이어 보냅니다.
2. **정리** — 프로그램이 맞춥니다.
   - 공식 발표 시각(`src`: 현지 시간대 · 날짜 · 시각)을 한국 시각으로 바꾸고 요일을 다시 셉니다.
   - 이미 지난 일정 · 2주 밖 일정을 빼고, 7개를 넘으면 오늘 · 내일의 2점 이상은 지키며 점수 낮은 · 늦은 것부터 뺍니다.
   - 출처는 이번 검색 · 가져오기 결과에 실제로 나온 주소만 남깁니다.
   - 줄 길이(60자) · `why`(30자) · 이모지 · 굵게 · 링크 · `±0.05%` 안쪽 숫자(→ '보합')를 검사합니다.
3. **다시 쓰기** — 고칠 것이 남으면 도구 없이 한 번 고쳐 쓰게 합니다(구조화 출력). 그래도 남으면 파일을 쓰지 않습니다.
4. **쓰기** — `--out`에 브리핑, `--log`에 실행 기록.

## 비용을 묶는 것

요청 하나에 검색 30 · 가져오기 10번, 조사 요청 8번, 검색 전체 70번, 어림 비용 상한(`--max-usd`)을 넘으면 더 찾지 않고 그때까지 확인한 것으로 마무리합니다. 웹 가져오기는 investing.com · tradingview.com · stooq.com · naver.com 을 막아 둡니다.

## 모드 (워크플로의 `MODE`)

- `"off"` — 예약 실행은 쉬고, Actions 화면의 **Run workflow**로만 돌립니다. 처음 상태입니다.
- `trial` — 매일 06:37(한국 시간)에 돌려 `api-runs` 브랜치에 `briefing_api.json`을 올립니다. 지금 판과 견주는 기간입니다.
- `live` — `main`의 `briefing.json`을 바꿉니다. 클라우드 예약 작업을 멈춘 뒤에만 바꿉니다. 07:13에 오늘 것이 없으면 한 번 더 돌립니다.

실행 기록은 모드와 상관없이 `api-runs` 브랜치의 `logs/`에 남습니다(토큰 · 검색 수 · 검색어 · 경고). 공개 저장소라 달러 어림은 남기지 않습니다(`--hide-usd`). 실제 비용은 Claude Console의 사용량 화면이 기준입니다.

## 키

저장소 **Settings → Secrets and variables → Actions**의 `ANTHROPIC_API_KEY`. 프로그램은 값이 있는지만 보고 찍지 않습니다. 워크플로의 쓰기 권한은 이 저장소(`contents: write`)뿐이라, 폰이 받아 실행하는 프로그램 저장소에는 쓸 수 없습니다.

## 종료 코드

`0` 성공 · `1` 예상 못 한 오류 · `2` 검사 불합격(파일 안 씀) · `3` API가 멈춤(거절 · 출력 한도) · `4` 키 없음

## 손으로

```bash
python -m unittest discover -s api
python api/briefing_api.py --check briefing.json --now 2026-11-16T06:23+09:00
```

첫 줄은 SDK가 없으면 `test_sdk_flow.py`(진짜 SDK를 거치는 6개)를 건너뜁니다. 그것까지 보려면 `pip install -r api/requirements.txt` 뒤 같은 명령을 돌립니다(Windows는 시간대 자료 `tzdata`도 깔아야 함). Actions에서는 늘 돕니다.

두 번째 줄은 API를 부르지 않고 이미 있는 브리핑을 같은 검사에 넣어 봅니다.

## 실패한 날

Actions 화면에서 **Run workflow**로 다시 돌립니다. 예약 작업이 남아 있는 동안은 예약 작업을 지금 실행해도 됩니다. 폰은 날짜가 오늘이 아닌 `briefing.json`은 쓰지 않습니다.
