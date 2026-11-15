"""SDK 수준 시험 — 진짜 anthropic SDK 를 거치되, HTTP 는 가짜 전송(httpx2.MockTransport)이 받는다.

API 를 부르지 않고 키도 쓰지 않는다(더미 문자열). 보는 것:
  - SDK 가 우리 인자를 받아들이는가
  - 실제로 나가는 요청(주소 · 헤더 · 본문)이 설계와 같은가
  - SDK 가 만든 응답 객체를 우리 코드가 다루는가(이어 보내기 · 마무리 · 다시 쓰기 · 거절 · 대체 모델 경계 · HTTP 오류)
SDK 가 없는 로컬 python 에선 건너뛰고, GitHub Actions 에선 꼭 돈다(SDK 가 없으면 실패).
SDK 버전을 올리면 여기부터 다시 맞춘다(헤더 · API 버전 값이 바뀌면 일부러 걸리게 해 둠).
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import briefing_api as B

try:
    import anthropic
    import httpx2
except ImportError:
    anthropic = httpx2 = None

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
API_VERSION = "2026-10-21"   # anthropic 2.3.0 이 보내는 anthropic-version
NOW = dt.datetime.fromisoformat("2026-11-16T06:40+09:00")   # 월요일 · 미국은 11/15 일요일(표준시)
C = B.context(NOW)
URL1 = "https://www.reuters.com/markets/us/stocks-close-2026-11-13/"
URL2 = "https://www.cnbc.com/2026/11/13/stock-market-today.html"
URL3 = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
EVIDENCE = "The S&P 500 rose 0.91% to 6,870.25. The 10-year yield fell 0.05 points to 4.12%."


# ── 가짜 서버 ────────────────────────────────────────────────

def sse(events):
    return "".join("event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e, ensure_ascii=False)) for e in events).encode()


class Server:
    """요청을 적어 두고 대본대로 SSE 응답(또는 오류)을 돌려준다. 인증 헤더는 이름만 적는다."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def handler(self, request):
        self.calls.append({
            "method": request.method, "host": request.url.host, "path": request.url.path,
            "query_names": sorted(request.url.params.keys()),
            "beta": request.headers.get("anthropic-beta"), "version": request.headers.get("anthropic-version"),
            "auth_names": sorted(k.lower() for k in request.headers.keys() if "key" in k.lower() or "auth" in k.lower()),
            "body": json.loads(request.content.decode("utf-8")),
        })
        if not self.script:
            return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "대본 끝"}})
        status, payload = self.script.pop(0)
        if status != 200:
            return httpx2.Response(status, json=payload)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(payload))


# ── 응답 대본 ────────────────────────────────────────────────

def th(t, sig):
    return {"type": "thinking", "thinking": t, "signature": sig}


def stu(i, name, inp):
    return {"type": "server_tool_use", "id": i, "name": name, "input": inp}


def wsr(i, urls):
    return {"type": "web_search_tool_result", "tool_use_id": i,
            "content": [{"type": "web_search_result", "url": u, "title": "제목", "encrypted_content": "ENC%d" % k,
                         "page_age": "1 day ago"} for k, u in enumerate(urls)]}


def wfr(i, url, text):
    return {"type": "web_fetch_tool_result", "tool_use_id": i,
            "content": {"type": "web_fetch_result", "url": url, "retrieved_at": "2026-11-15T21:30:00Z",
                        "content": {"type": "document", "title": "문서", "citations": {"enabled": True},
                                    "source": {"type": "text", "media_type": "text/plain", "data": text}}}}


def tx(t, citations=None):
    return {"type": "text", "text": t, "citations": citations or []}


def fb(frm, to):
    return {"type": "fallback", "from": {"model": frm}, "to": {"model": to}, "trigger": {"type": "refusal"}}


def message(blocks, stop, *, model=B.MODEL, searches=0, fetches=0, stop_details=None, iterations=None,
            inp=1500, cw=6000, cr=0, out=900):
    """블록 목록 → 실제 스트림과 같은 순서의 SSE 이벤트(텍스트 · 생각 · 도구 입력은 델타로, 결과 · 경계는 통째로)."""
    ev = [{"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": 1}}}]
    for i, b in enumerate(blocks):
        t = b["type"]
        if t == "text":
            ev.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}})
            for ci in b.get("citations") or []:
                ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "citations_delta", "citation": ci}})
        elif t == "thinking":
            ev.append({"type": "content_block_start", "index": i,
                       "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": b["thinking"]}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}})
        elif t == "server_tool_use":
            ev.append({"type": "content_block_start", "index": i,
                       "content_block": {"type": "server_tool_use", "id": b["id"], "name": b["name"], "input": {}}})
            ev.append({"type": "content_block_delta", "index": i,
                       "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"], ensure_ascii=False)}})
        else:
            ev.append({"type": "content_block_start", "index": i, "content_block": b})
        ev.append({"type": "content_block_stop", "index": i})
    usage = {"output_tokens": out, "input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
             "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": fetches}}
    if iterations:
        usage["iterations"] = iterations
    ev.append({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None, "stop_details": stop_details},
               "usage": usage})
    ev.append({"type": "message_stop"})
    return (200, ev)


def fenced(obj):
    return "```json\n" + json.dumps(obj, ensure_ascii=False, indent=1) + "\n```"


SCHED = [   # when 의 요일은 일부러 틀림 — src 로 다시 세는지 본다
    {"when": "11/17 (월) 22:30", "what": "미국 엠파이어스테이트 제조업지수", "impact": 2, "why": "제조업 경기의 첫 신호",
     "src": {"tz": "America/New_York", "date": "2026-11-17", "time": "08:30"}},
    {"when": "11/19 (수) 04:00", "what": "FOMC 의사록 공개", "impact": 4, "why": "금리 경로 단서",
     "src": {"tz": "America/New_York", "date": "2026-11-18", "time": "14:00"}},
    {"when": "11/20 (목) 22:30", "what": "미국 신규 실업수당 청구건수", "impact": 2, "why": "고용 둔화 여부",
     "src": {"tz": "America/New_York", "date": "2026-11-20", "time": "08:30"}},
]
GOOD = {"overnight": ["S&P500 0.91% 올라 6,870.25 마감", "나스닥 1.20% 올라 23,456.78",
                      "미국 10년물 금리 4.12%로 0.05%p 내림"],
        "schedule": SCHED, "sources": [URL1, URL2, URL3, "https://unseen.example.com/x"], "dropped": ["예시 한 줄"]}


def types_of(content):
    return [x.get("type") for x in content]


def nulls(o, path=""):
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            out += ["%s.%s" % (path, k)] if v is None else nulls(v, "%s.%s" % (path, k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            out += nulls(v, "%s[%d]" % (path, i))
    return out


@unittest.skipIf(anthropic is None and not IN_ACTIONS, "anthropic SDK 가 없어 건너뜀(액션에선 돎)")
class SdkFlowTest(unittest.TestCase):
    def setUp(self):
        if anthropic is None:
            self.fail("GitHub Actions 에선 anthropic SDK 가 깔려 있어야 함")
        self.tmp = Path(tempfile.mkdtemp())
        self.rules = B.RULES_FILE.read_text(encoding="utf-8")

    def go(self, script, *extra):
        server = Server(script)
        client = anthropic.Anthropic(api_key="dummy-key-for-offline-test", max_retries=0,
                                     http_client=httpx2.Client(transport=httpx2.MockTransport(server.handler)))
        out, logp = self.tmp / "b.json", self.tmp / "log.json"
        args = B.parse_args(["--out", str(out), "--log", str(logp), *extra])
        with mock.patch("builtins.print"), mock.patch("traceback.print_exc"):
            code = B.run_brief(client, args, C, rules=self.rules)
        log = json.loads(logp.read_text(encoding="utf-8"))
        brief = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
        return code, server.calls, log, brief

    def common(self, call):
        self.assertEqual((call["method"], call["host"], call["path"], call["query_names"]),
                         ("POST", "api.anthropic.com", "/v1/messages", []))
        self.assertEqual(call["beta"], ",".join(B.BETAS))
        self.assertEqual(call["version"], API_VERSION)
        self.assertEqual(call["auth_names"], ["authorization"])   # 2.x 는 키를 Authorization 헤더로 보냄(값은 안 봄)
        b = call["body"]
        self.assertEqual((b.get("model"), b.get("stream"), b.get("fallbacks")), (B.MODEL, True, "default"))
        self.assertFalse(set(b) & {"temperature", "top_p", "top_k", "thinking", "output_format", "betas"})

    def test_pause_then_end(self):
        r1 = [th("생각 1", "sig1"), stu("srvtoolu_01", "web_search", {"query": "S&P 500 close November 13"}),
              wsr("srvtoolu_01", [URL1]), stu("srvtoolu_02", "web_fetch", {"url": URL2}),
              wfr("srvtoolu_02", URL2, EVIDENCE), stu("srvtoolu_03", "web_search", {"query": "FOMC minutes date"})]
        r2 = [wsr("srvtoolu_03", [URL3]), th("생각 2", "sig2"),
              tx("밤사이 정리.", [{"type": "web_search_result_location", "url": URL1, "title": "제목",
                                   "encrypted_index": "EI", "cited_text": "Nasdaq gained 1.20% to 23,456.78"}]),
              tx(fenced(GOOD))]
        code, calls, log, brief = self.go([message(r1, "pause_turn", searches=2, fetches=1),
                                           message(r2, "end_turn", searches=1, cr=6000, cw=800)], "--hide-usd")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.common(call)
        b1, b2 = calls[0]["body"], calls[1]["body"]
        self.assertEqual(b1["output_config"], {"effort": "high"})
        self.assertEqual(b1["cache_control"], {"type": "ephemeral"})
        self.assertEqual(b1["max_tokens"], 64000)
        self.assertEqual(b1["system"], self.rules)
        self.assertEqual(b1["tools"], B.tools(30, 10))
        self.assertNotIn("tool_choice", b1)
        self.assertEqual([m["role"] for m in b2["messages"]], ["user", "assistant"])   # 새 user 없이 이어 보냄
        sent = b2["messages"][1]["content"]
        self.assertEqual(types_of(sent), ["thinking", "server_tool_use", "web_search_tool_result", "server_tool_use",
                                          "web_fetch_tool_result", "server_tool_use"])
        self.assertEqual(sent[0], th("생각 1", "sig1"))
        self.assertEqual(sent[1], stu("srvtoolu_01", "web_search", {"query": "S&P 500 close November 13"}))
        self.assertEqual(sent[2], wsr("srvtoolu_01", [URL1]))
        self.assertEqual(sent[4], wfr("srvtoolu_02", URL2, EVIDENCE))
        self.assertEqual(nulls(b2), [])
        self.assertEqual(brief["sources"], [URL1, URL2, URL3])
        self.assertEqual([s["when"] for s in brief["schedule"]], ["11/17 (화) 22:30", "11/19 (목) 04:00", "11/20 (금) 22:30"])
        t = log["totals"]
        self.assertEqual((t["requests"], t["web_search_requests"], t["web_fetch_requests"], t["cache_read_input_tokens"]),
                         (2, 3, 1, 6000))
        self.assertNotIn('"usd', json.dumps(log))
        self.assertEqual(log["caps"]["max_usd"], 3.0)
        self.assertEqual(log["queries"], ["S&P 500 close November 13", "FOMC minutes date"])
        self.assertEqual(log["fetched"], [URL2])
        self.assertEqual(log["numbers"]["not_found"], [])   # 인용 글 · 가져온 본문이 근거로 모임
        self.assertEqual(log["dropped"], ["예시 한 줄"])
        self.assertEqual(log["served_by"], [B.MODEL])

    def test_budget_finalize(self):
        r1 = [th("생각 1", "sig1"), stu("srvtoolu_01", "web_search", {"query": "q1"}), wsr("srvtoolu_01", [URL1]),
              th("생각 1b", "sig1b"), stu("srvtoolu_02", "web_search", {"query": "q2"})]
        r2 = [th("생각 2", "sig2"), tx(fenced(dict(GOOD, sources=[URL1])))]
        code, calls, log, brief = self.go([message(r1, "pause_turn", searches=1), message(r2, "end_turn")],
                                          "--max-usd", "0.0001")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.common(calls[1])
        b1, b2 = calls[0]["body"], calls[1]["body"]
        self.assertEqual(b2["tool_choice"], {"type": "none"})
        self.assertEqual(b2["tools"], b1["tools"])     # 도구 목록 · system 은 그대로(앞부분을 바꾸지 않음)
        self.assertEqual(b2["system"], b1["system"])
        self.assertEqual(b2["messages"][0], b1["messages"][0])
        self.assertEqual([m["role"] for m in b2["messages"]], ["user", "assistant", "user"])
        self.assertEqual(types_of(b2["messages"][1]["content"]), ["thinking", "server_tool_use", "web_search_tool_result"])
        self.assertEqual(b2["messages"][2]["content"], B.FINALIZE_MSG)
        self.assertTrue(any("예산에 닿아" in w for w in log["warnings"]))
        self.assertIn("usd", log["totals"])            # 비공개 모드에선 달러 어림이 남음

    def test_repair_structured(self):
        bad = dict(GOOD, overnight=["가" * 70, "나스닥 1.20% 올라 23,456.78", "미국 10년물 금리 4.12%"])
        fixed = dict(GOOD, schedule=[{k: v for k, v in s.items() if k != "src"} for s in SCHED])
        code, calls, log, brief = self.go([message([tx(fenced(bad))], "end_turn"),
                                           message([tx(json.dumps(fixed, ensure_ascii=False))], "end_turn")])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.common(calls[1])
        b2 = calls[1]["body"]
        self.assertEqual(b2["output_config"], {"effort": "medium",
                                               "format": {"type": "json_schema", "schema": B.REPAIR_SCHEMA}})
        for k in ("tools", "tool_choice", "cache_control"):
            self.assertNotIn(k, b2)
        self.assertEqual((b2["max_tokens"], len(b2["messages"])), (16000, 1))
        self.assertIn("70자", b2["messages"][0]["content"])
        self.assertTrue(brief["overnight"][0].startswith("S&P500"))

    def test_refusal(self):
        sd = {"type": "refusal", "category": "cyber", "explanation": "시험용 설명",
              "fallback_credit_token": "tok_dummy", "fallback_has_prefill_claim": False, "recommended_model": None}
        code, calls, log, brief = self.go([message([], "refusal", stop_details=sd)])
        self.assertEqual(code, 3)
        self.assertIsNone(brief)
        self.assertTrue(any("거절(refusal · cyber)" in e for e in log["errors"]))
        self.assertEqual(log["requests"][0]["stop_details"],
                         {"type": "refusal", "category": "cyber", "explanation": "시험용 설명"})   # 크레딧 토큰은 안 남김

    def test_fallback_boundary(self):
        r1 = [th("a", "sig_a"), stu("srvtoolu_01", "web_search", {"query": "q1"}), wsr("srvtoolu_01", [URL1]),
              stu("srvtoolu_02", "web_search", {"query": "q2"}), fb(B.MODEL, "claude-sonnet-5-5"),
              th("b", "sig_b"), stu("srvtoolu_04", "web_fetch", {"url": URL2})]
        its = [{"type": "message", "model": B.MODEL, "input_tokens": 1500, "output_tokens": 300,
                "cache_creation_input_tokens": 6000, "cache_read_input_tokens": 0},
               {"type": "fallback_message", "model": "claude-sonnet-5-5", "input_tokens": 1700, "output_tokens": 600,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}]
        r2 = [wfr("srvtoolu_04", URL2, EVIDENCE), tx(fenced(dict(GOOD, sources=[URL1, URL2])))]
        code, calls, log, brief = self.go([message(r1, "pause_turn", searches=1, iterations=its),
                                           message(r2, "end_turn", model="claude-sonnet-5-5", fetches=1)])
        self.assertEqual(code, 0)
        sent = calls[1]["body"]["messages"][1]["content"]
        self.assertEqual(types_of(sent), ["server_tool_use", "web_search_tool_result", "fallback", "thinking",
                                          "server_tool_use"])   # 경계 앞 thinking · 짝 없는 검색만 뺌
        self.assertEqual(sent[2], fb(B.MODEL, "claude-sonnet-5-5"))   # 'from' 키 그대로(from_ 아님)
        r0 = log["requests"][0]
        self.assertEqual((r0["model"], r0["iterations"], r0["fallback_ran"]), ("claude-sonnet-5-5", 2, True))
        self.assertIn("usd_iterations_sum", r0)
        self.assertEqual(log["served_by"], ["claude-sonnet-5-5"])

    def test_http_400(self):
        code, calls, log, brief = self.go([(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                              "message": "시험용 오류"}})])
        self.assertEqual(code, 1)
        self.assertIsNone(brief)
        self.assertTrue(log["errors"] and log["errors"][0].startswith("BadRequestError"))


if __name__ == "__main__":
    unittest.main()
