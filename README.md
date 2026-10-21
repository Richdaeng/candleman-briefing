# candleman-briefing

디스코드 서버 「캔들맨의 실시간 포지션」의 `#일일-경제-브리핑`에 쓰는 **`briefing.json` 하나만 두는 저장소**입니다.

- **누가 올리나:** Claude 클라우드 예약 작업 「캔들맨 아침 경제 브리핑」이 매일 06:40(한국 시간)에 웹에서 밤사이 소식 · 다가오는 일정을 찾아 `briefing.json`을 이 저장소 `main`에 올립니다(Opus 5.5 · 추론 강도 max).
- **누가 가져가나:** 서브폰 프로그램 [candleman-alert](https://github.com/Richdaeng/candleman-alert)가 08:00에 아래 주소에서 가져와 신용 · 미수 숫자와 함께 카드로 올립니다. 날짜가 오늘이 아니면 쓰지 않습니다.
  `https://raw.githubusercontent.com/Richdaeng/candleman-briefing/refs/heads/main/briefing.json`
- **왜 따로 두나:** 예약 작업에 GitHub 쓰기 권한(Claude GitHub 앱)을 주는 곳은 이 저장소뿐입니다. 작업이 잘못되더라도 폰이 매일 자동으로 받아 실행하는 프로그램 코드(candleman-alert)에는 쓸 수 없습니다.
- **`.claude/settings.json`:** 클라우드 세션의 추론 강도를 max로 둡니다(`CLAUDE_CODE_EFFORT_LEVEL`).

## briefing.json 모양

```json
{
  "date": "YYYY-MM-DD",
  "overnight": ["한 줄", "한 줄"],
  "schedule": [{"when": "MM/DD (요일)", "what": "무슨 일", "impact": 4, "why": "과거 영향 한 줄"}],
  "sources": ["https://..."]
}
```

`impact`(1~5)는 2016~2026년 시세로 잰 표를 따릅니다. 폰이 별(★)로 그립니다.
