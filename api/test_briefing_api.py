"""briefing_api 오프라인 시험 — API 를 부르지 않고 가짜 클라이언트로 흐름 · 검사를 본다.

돌리기: python -I -X utf8 -m unittest discover -s api -v
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import briefing_api as B

KST = B.KST


def at(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=KST)


NOW = at("2026-11-16 06:23")          # 월요일 · 미국은 11/15 일요일 16:23 (표준시)
C = B.context(NOW)


class Obj(dict):
    """SDK 객체 흉내: 속성으로도 읽히고 to_dict() 가 있음."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)

    def to_dict(self):
        return json.loads(json.dumps(self, ensure_ascii=False))


def text(s, citations=None):
    b = Obj(type="text", text=s)
    if citations:
        b["citations"] = citations
    return b


def stu(i, name="web_search", **inp):
    return Obj(type="server_tool_use", id=i, name=name, input=inp)


def wsr(i, urls):
    return Obj(type="web_search_tool_result", tool_use_id=i,
               content=[Obj(type="web_search_result", url=u, title="t", encrypted_content="xx", page_age="p") for u in urls])


def wfr(i, url, data):
    return Obj(type="web_fetch_tool_result", tool_use_id=i,
               content=Obj(type="web_fetch_result", url=url, retrieved_at="r",
                           content=Obj(type="document", source=Obj(type="text", media_type="text/plain", data=data))))


def usage(inp=1000, out=500, cw=0, cr=0, searches=0, fetches=0):
    return Obj(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=cw, cache_read_input_tokens=cr,
               server_tool_use=Obj(web_search_requests=searches, web_fetch_requests=fetches))


def msg(content, stop="end_turn", u=None, stop_details=None):
    m = Obj(content=content, stop_reason=stop, usage=u or usage(), model=B.MODEL)
    if stop_details:
        m["stop_details"] = stop_details
    return m


def fenced(obj, before="조사를 마쳤다.\n"):
    return before + "```json\n" + json.dumps(obj, ensure_ascii=False, indent=1) + "\n```"


URLS = ["https://a.example/1", "https://b.example/2", "https://c.example/3"]


def answer(**over):
    d = {
        "overnight": ["미국 증시 휴장 (주말) · 11/13 마감 나스닥 −0.91% · S&P500 −0.40%",
                      "비트코인 $95,123 (+2.1%) · 이더리움 $3,456 (−1.0%)",
                      "달러/원 1,385.0원 (11/13 종가) · 미 10년물 4.12%"],
        "schedule": [
            {"when": "11/18 (수) 08:30", "what": "미국 10월 CPI 발표", "impact": 4, "why": "과거 발표 날 미국 주식 1.39배",
             "src": {"tz": "America/New_York", "date": "2026-11-18", "time": "08:30", "approx": False}},
            {"when": "11/19 (목) 04:00", "what": "미국 FOMC 의사록 공개", "impact": 2, "why": "재지 않음"},
            {"when": "11/26 (목)", "what": "한국은행 금통위 기준금리 결정", "impact": 2, "why": "평소와 뚜렷한 차이 없음"},
        ],
        "sources": list(URLS),
        "dropped": ["다우 등락률 (두 출처가 다름)"],
    }
    d.update(over)
    return d


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        outer = self

        class _Stream:
            def __init__(self, m):
                self.m = m

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return self.m

        class _Messages:
            def stream(self, **kw):
                outer.calls.append(kw)
                item = outer.script.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return _Stream(item)

        self.beta = SimpleNamespace(messages=_Messages())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out, self.log = self.tmp / "b.json", self.tmp / "log.json"

    def args(self, *extra):
        return B.parse_args(["--out", str(self.out), "--log", str(self.log), *extra])

    def run_with(self, script, *extra):
        client = FakeClient(script)
        with mock.patch("builtins.print"):
            code = B.run_brief(client, self.args(*extra), C, rules=B.RULES_FILE.read_text(encoding="utf-8"))
        log = json.loads(self.log.read_text(encoding="utf-8"))
        return code, client, log


class ContextTest(unittest.TestCase):
    def test_weekend_winter(self):
        self.assertEqual(C["today"], dt.date(2026, 11, 16))
        self.assertEqual(C["tomorrow"], dt.date(2026, 11, 17))
        self.assertEqual(C["end"], dt.date(2026, 11, 30))
        self.assertFalse(C["dst"])
        self.assertEqual(C["close_kst"], "06:00")
        self.assertTrue(C["session"].startswith("없음"))
        self.assertIn("2026-11-13 (금)", C["session"])
        u = B.user_message(C)
        self.assertIn("2026-11-16 (월)", u)
        self.assertIn("2026-11-15 (일) 16:23", u)
        self.assertIn("fomccalendars", u)

    def test_weekday_summer(self):
        c = B.context(at("2026-07-15 06:23"))
        self.assertTrue(c["dst"])
        self.assertEqual(c["close_kst"], "05:00")
        self.assertIn("2026-07-14 (화)", c["session"])
        self.assertIn("05:00", c["session"])

    def test_before_us_close(self):   # 미국 16시 전에 돌리면 그 전날 장이 '지난밤'
        c = B.context(at("2026-11-17 05:30"))   # 미국 11/16 월 15:30
        self.assertIn("2026-11-13 (금)", c["session"])   # 11/15 일 → 주말 → 마지막 거래일 금요일


class WhenTest(unittest.TestCase):
    def conv(self, tz, d, t, approx=False):
        return B.format_when(B.from_src({"tz": tz, "date": d, "time": t, "approx": approx}))

    def test_src_to_kst(self):
        self.assertEqual(self.conv("America/New_York", "2026-11-18", "08:30"), "11/18 (수) 22:30")
        self.assertEqual(self.conv("America/New_York", "2026-12-09", "14:00"), "12/10 (목) 04:00")
        self.assertEqual(self.conv("America/New_York", "2026-07-29", "14:00"), "07/30 (목) 03:00")   # 서머타임
        self.assertEqual(self.conv("America/New_York", "2026-11-18", "16:00", True), "11/19 (목) 06시쯤")
        self.assertEqual(self.conv("Asia/Seoul", "2026-11-26", "10:00"), "11/26 (목) 10:00")
        self.assertEqual(self.conv("Europe/London", "2026-11-19", "07:00"), "11/19 (목) 16:00")

    def test_bad_src(self):
        for src in ({"tz": "Mars/Base", "date": "2026-11-18", "time": "08:30"},
                    {"tz": "America/New_York", "date": "2026-11-31", "time": "08:30"},
                    {"tz": "America/New_York", "date": "2026-11-18", "time": "8시"},
                    {"tz": "America/New_York", "date": "2026-11-18", "time": "24:10"},
                    {"date": "2026-11-18", "time": "08:30"}):
            with self.assertRaises((KeyError, ValueError, TypeError)):
                B.from_src(src)

    def test_parse_formats(self):
        t = C["today"]
        self.assertEqual(B.parse_when("11/17 (화) 22:30", t)["kind"], "exact")
        self.assertEqual(B.parse_when("11/18 (수) 06시쯤", t)["hh"], 6)
        self.assertEqual(B.parse_when("11/19 (목) 밤", t)["label"], "밤")
        self.assertEqual(B.parse_when("11/26 (목)", t)["kind"], "date")
        self.assertEqual(B.parse_when("11/26", t)["date"], dt.date(2026, 11, 26))
        self.assertEqual(B.parse_when("01/05 (화)", t)["date"], dt.date(2027, 1, 5))   # 해 넘김
        for bad in ("11/31 (월)", "11/17 (화) 25:00", "11월 17일", "", None, "11/17 (화) 밤 10시"):
            self.assertIsNone(B.parse_when(bad, t), bad)
        self.assertEqual(B.format_when(B.parse_when("11/18 (수) 6시쯤", t)), "11/18 (수) 06시쯤")


class NormalizeTest(unittest.TestCase):
    def norm(self, obj, seen=None):
        return B.normalize(obj, C, seen)

    def test_good(self):
        brief, errs, warns = self.norm(answer())
        self.assertEqual(errs, [])
        self.assertEqual(brief["date"], "2026-11-16")
        self.assertEqual(brief["schedule"][0]["when"], "11/18 (수) 22:30")   # src 로 다시 셈
        self.assertTrue(any("src 로 다시 셈" in w for w in warns))
        self.assertEqual(set(brief), {"date", "overnight", "schedule", "sources"})
        self.assertEqual(set(brief["schedule"][0]), {"when", "what", "impact", "why"})

    def test_weekday_fixed(self):
        a = answer()
        a["schedule"][1]["when"] = "11/19 (수) 04:00"
        brief, errs, warns = self.norm(a)
        self.assertEqual(errs, [])
        self.assertEqual(brief["schedule"][1]["when"], "11/19 (목) 04:00")
        self.assertTrue(any("요일을 고침" in w for w in warns))

    def test_past_and_far(self):
        sched = [
            {"when": "11/16 (월) 05:00", "what": "지난 일", "impact": 2, "why": "x"},
            {"when": "11/15 (일)", "what": "어제 일", "impact": 2, "why": "x"},
            {"when": "12/01 (화)", "what": "2주 밖", "impact": 4, "why": "x"},
            {"when": "11/16 (월) 21:00", "what": "오늘 밤", "impact": 2, "why": "x"},
            {"when": "11/16 (월)", "what": "오늘 시각 모름", "impact": 2, "why": "x"},
            {"when": "11/30 (월)", "what": "마지막 날", "impact": 2, "why": "x"},
        ]
        brief, errs, warns = self.norm(answer(schedule=sched))
        self.assertEqual(errs, [])
        self.assertEqual([x["what"] for x in brief["schedule"]], ["오늘 밤", "오늘 시각 모름", "마지막 날"])
        self.assertEqual(sum("뺌" in w for w in warns), 3)

    def test_trim_keeps_today_tomorrow(self):
        sched = [
            {"when": "11/16 (월) 21:00", "what": "오늘 1점", "impact": 1, "why": "x"},
            {"when": "11/17 (화) 22:30", "what": "내일 2점", "impact": 2, "why": "x"},
            {"when": "11/18 (수) 22:30", "what": "A 4점", "impact": 4, "why": "x"},
            {"when": "11/19 (목) 04:00", "what": "B 1점", "impact": 1, "why": "x"},
            {"when": "11/20 (금)", "what": "C 2점", "impact": 2, "why": "x"},
            {"when": "11/24 (화)", "what": "D 1점", "impact": 1, "why": "x"},
            {"when": "11/25 (수) 22:30", "what": "E 2점", "impact": 2, "why": "x"},
            {"when": "11/26 (목)", "what": "F 2점", "impact": 2, "why": "x"},
            {"when": "11/27 (금)", "what": "G 3점", "impact": 3, "why": "x"},
        ]
        brief, errs, warns = self.norm(answer(schedule=sched))
        self.assertEqual(errs, [])
        names = [x["what"] for x in brief["schedule"]]
        self.assertEqual(len(names), 7)
        self.assertIn("내일 2점", names)
        self.assertNotIn("D 1점", names)      # 1점 중 가장 늦은 것부터
        self.assertNotIn("B 1점", names)
        self.assertIn("오늘 1점", names)      # 1점이지만 가장 이른 1점이라 남음(자리가 남음)
        self.assertEqual(sum("넘어 뺌" in w for w in warns), 2)

    def test_overnight_problems(self):
        lines = ["가" * 61, "나스닥 **급등**", "비트코인 🚀", "나스닥 +0.0% · 다우 −0.03%", "정상 줄"]
        brief, errs, _ = self.norm(answer(overnight=lines))
        joined = "\n".join(errs)
        self.assertIn("61자", joined)
        self.assertIn("굵게", joined)
        self.assertIn("이모지", joined)
        self.assertEqual(sum("보합" in e for e in errs), 2)
        _, errs, _ = self.norm(answer(overnight=["줄"] * 7))
        self.assertTrue(any("7줄" in e for e in errs))
        _, errs, warns = self.norm(answer(overnight=["줄 하나", "줄 둘"]))
        self.assertEqual(errs, [])
        self.assertTrue(any("2줄" in w for w in warns))
        _, errs, _ = self.norm(answer(overnight=[]))
        self.assertTrue(any("비어" in e for e in errs))

    def test_schedule_problems(self):
        sched = [{"when": "다음 주", "what": "x", "impact": 4, "why": "y"},
                 {"when": "11/18 (수)", "what": "", "impact": 9, "why": "가" * 31},
                 {"when": "11/19 (목)", "what": "z", "impact": True, "why": "w"},
                 "글로 된 일정"]
        _, errs, _ = self.norm(answer(schedule=sched))
        joined = "\n".join(errs)
        for piece in ("when 모양", "what 이 비어", "impact 는 1~5", "why 가 31자", "객체가 아님"):
            self.assertIn(piece, joined)
        brief, errs, _ = self.norm(answer(schedule=[{"when": "11/18 (수)", "what": "x", "impact": "3", "why": "y"}]))
        self.assertEqual(errs, [])
        self.assertEqual(brief["schedule"][0]["impact"], 3)

    def test_bad_src_falls_back_to_when(self):
        a = answer()
        a["schedule"][0]["src"] = {"tz": "Mars/Base", "date": "2026-11-18", "time": "08:30"}
        a["schedule"][0]["when"] = "11/18 (수) 22:30"
        brief, errs, warns = self.norm(a)
        self.assertEqual(errs, [])
        self.assertEqual(brief["schedule"][0]["when"], "11/18 (수) 22:30")
        self.assertTrue(any("src 를 못 읽어" in w for w in warns))

    def test_sources(self):
        a = answer(sources=URLS + ["https://a.example/1/", "ftp://x", "https://z.example/9"])
        brief, errs, warns = self.norm(a, seen={"https://A.example/1", "https://b.example/2/", "https://c.example/3"})
        self.assertEqual(errs, [])
        self.assertEqual(brief["sources"], URLS + ["https://a.example/1/"])
        self.assertTrue(any("z.example" in w for w in warns))
        brief, _, warns = self.norm(answer(sources=["https://s%d.example" % i for i in range(12)]))
        self.assertEqual(len(brief["sources"]), 10)

    def test_not_dict(self):
        _, errs, _ = self.norm(["x"])
        self.assertTrue(errs)


class ExtractTest(unittest.TestCase):
    def test_fenced_and_raw(self):
        a = answer()
        self.assertEqual(B.extract_json(fenced(a)), a)
        two = fenced({"overnight": ["옛 초안"]}) + "\n고침:\n" + fenced(a)
        self.assertEqual(B.extract_json(two), a)
        broken = "```json\n{\"overnight\": [\n```\n그래서 다시: " + json.dumps(a, ensure_ascii=False)
        self.assertEqual(B.extract_json(broken), a)
        self.assertEqual(B.extract_json("앞말 " + json.dumps(a, ensure_ascii=False) + " 뒷말"), a)
        self.assertIsNone(B.extract_json("JSON 없음 {이건 아님}"))
        self.assertIsNone(B.extract_json(None))

    def test_final_text_tail(self):
        content = [text("찾아보겠다."), stu("s1", query="q"), wsr("s1", URLS),
                   text("```json\n{\"overnight\": "), text("[\"줄\"], \"schedule\": []}\n```", citations=[{"url": URLS[0], "cited_text": "c"}])]
        tail, whole = B.final_text(content)
        self.assertTrue(tail.startswith("```json"))
        self.assertTrue(whole.startswith("찾아보겠다."))
        self.assertEqual(B.extract_json(tail), {"overnight": ["줄"], "schedule": []})


class EchoTest(unittest.TestCase):
    def test_no_fallback_unchanged(self):
        content = [Obj(type="thinking", thinking="", signature="s"), stu("s1", query="q"), wsr("s1", URLS), text("x")]
        self.assertEqual(B.echo_blocks(content), content)

    def test_fallback_boundary(self):
        th1 = Obj(type="thinking", thinking="", signature="s")
        lone = stu("s9", query="q9")
        content = [th1, stu("s1", query="q"), wsr("s1", URLS), Obj(type="tool_use", id="t", name="x", input={}),
                   lone, Obj(type="mystery"), text("앞 글"), Obj(type="fallback", **{"from": {"model": "a"}, "to": {"model": "b"}}),
                   Obj(type="thinking", thinking="", signature="s2"), text("뒤 글")]
        out = B.echo_blocks(content)
        types = [b["type"] for b in out]
        self.assertEqual(types, ["server_tool_use", "web_search_tool_result", "text", "fallback", "thinking", "text"])
        self.assertNotIn(lone, out)

    def test_drop_unpaired(self):
        msgs = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": [stu("s1", query="a"), wsr("s1", URLS)]},
                {"role": "assistant", "content": [stu("s2", query="b"), text("x"), stu("s1b", query="c")]}]
        out = B.drop_unpaired(msgs)
        self.assertEqual([b["id"] for b in out[-1]["content"] if b["type"] == "server_tool_use"], [])
        self.assertEqual(len(out[-1]["content"]), 1)
        self.assertIs(out[1], msgs[1])

    def test_drop_unpaired_trailing_thinking(self):
        th = Obj(type="thinking", thinking="", signature="s")
        msgs = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": [text("a"), th, stu("s9", query="z")]}]
        self.assertEqual([b["type"] for b in B.drop_unpaired(msgs)[-1]["content"]], ["text"])   # 끝에 남는 thinking 도 뺌
        msgs = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": [stu("s1", query="a"), wsr("s1", URLS)]},
                {"role": "assistant", "content": [th, stu("s9", query="z")]}]
        out = B.drop_unpaired(msgs)
        self.assertEqual(len(out), 2)            # 다 빈 메시지는 뺌
        self.assertIs(out[1], msgs[1])
        only_user = [{"role": "user", "content": "q"}]
        self.assertEqual(B.drop_unpaired(only_user), only_user)


class FlowTest(Base):
    def research_msgs(self, final_obj, stop_first="pause_turn"):
        first = msg([Obj(type="thinking", thinking="", signature="s"), text("검색함"), stu("s1", query="나스닥 마감"),
                     wsr("s1", URLS), stu("f1", name="web_fetch", url=URLS[0]), wfr("f1", URLS[0], "나스닥 -0.91% S&P500 0.40% 6,812.5")],
                    stop=stop_first, u=usage(inp=20000, out=3000, cw=15000, searches=5, fetches=1))
        last = msg([text(fenced(final_obj))], u=usage(inp=2000, out=1500, cr=30000, searches=2))
        return first, last

    def one_shot(self, final_obj):
        """조사 한 번(end_turn)에 도구 블록 뒤 JSON 까지 다 들어 있는 응답."""
        first, last = self.research_msgs(final_obj, stop_first="end_turn")
        first["content"] = first["content"] + last["content"]
        return first

    def test_pause_then_end(self):
        first, last = self.research_msgs(answer())
        code, client, log = self.run_with([first, last])
        self.assertEqual(code, 0)
        out = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(out["date"], "2026-11-16")
        self.assertEqual(out["schedule"][0]["when"], "11/18 (수) 22:30")
        self.assertEqual(out["sources"], URLS)
        self.assertEqual(len(client.calls), 2)
        k0 = client.calls[0]
        self.assertEqual(k0["model"], "claude-opus-5-5")
        self.assertEqual(k0["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(k0["fallbacks"], "default")
        self.assertEqual(k0["output_config"], {"effort": "high"})
        self.assertEqual(k0["cache_control"], {"type": "ephemeral"})
        self.assertEqual([t["type"] for t in k0["tools"]], ["web_search_20260318", "web_fetch_20260318"])
        self.assertEqual(k0["tools"][0]["max_uses"], 30)
        self.assertNotIn("tool_choice", k0)
        self.assertEqual(len(k0["messages"]), 1)
        k1 = client.calls[1]
        self.assertEqual([m["role"] for m in k1["messages"]], ["user", "assistant"])
        self.assertEqual(k1["messages"][1]["content"], first["content"])   # 바꾸지 않고 되돌려 보냄
        self.assertEqual(log["code"], 0)
        self.assertEqual(log["totals"]["requests"], 2)
        self.assertEqual(log["totals"]["web_search_requests"], 7)
        self.assertEqual(log["queries"], ["나스닥 마감"])
        self.assertEqual(log["fetched"], [URLS[0]])
        self.assertEqual(log["dropped"], ["다우 등락률 (두 출처가 다름)"])
        self.assertEqual(log["brief"], out)
        nums = log["numbers"]
        self.assertEqual(nums["checked"], 8)              # S&P500 의 500 · 10년물의 10 · 날짜는 안 셈
        self.assertNotIn("0.91", nums["not_found"])      # 가져온 본문에 있음
        self.assertNotIn("0.40", nums["not_found"])
        self.assertIn("95,123", nums["not_found"])       # 본문에 없음 → 기록에 남음
        exp = (22000 * 4 + 4500 * 20 + 15000 * 5 + 30000 * 0.2) / 1e6 + 7 * 0.01
        self.assertAlmostEqual(log["totals"]["usd"], round(exp, 4), places=4)

    def test_refusal(self):
        code, client, log = self.run_with([msg([], stop="refusal", stop_details={"type": "refusal", "category": "cyber"})])
        self.assertEqual(code, 3)
        self.assertFalse(self.out.exists())
        self.assertIn("거절", log["errors"][0])
        self.assertIn("cyber", log["errors"][0])
        self.assertEqual(log["requests"][0]["stop_details"]["category"], "cyber")

    def test_max_tokens(self):
        code, _, log = self.run_with([msg([text("길게...")], stop="max_tokens")])
        self.assertEqual(code, 3)
        self.assertIn("출력 한도", log["errors"][0])

    def test_repair_success(self):
        bad = answer(overnight=["가" * 70, "둘째 줄", "셋째 줄"])
        good = answer(overnight=["가" * 50, "둘째 줄", "셋째 줄"])
        good.pop("schedule")
        good["schedule"] = [{"when": "11/18 (수) 22:30", "what": "미국 10월 CPI 발표", "impact": 4, "why": "과거 발표 날 1.39배"},
                            {"when": "11/19 (목) 04:00", "what": "FOMC 의사록", "impact": 2, "why": "재지 않음"},
                            {"when": "11/26 (목)", "what": "금통위", "impact": 2, "why": "평소와 비슷"}]
        first = self.one_shot(bad)
        repaired = msg([text(json.dumps(good, ensure_ascii=False))])
        code, client, log = self.run_with([first, repaired])
        self.assertEqual(code, 0)
        self.assertEqual(len(client.calls), 2)
        k = client.calls[1]
        self.assertEqual(k["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(k["output_config"]["effort"], "medium")
        self.assertNotIn("tools", k)
        self.assertIn("70자", k["messages"][0]["content"])
        self.assertTrue(log["repaired_because"])
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["overnight"][0], "가" * 50)

    def test_repair_still_bad(self):
        bad = answer(overnight=["가" * 70, "둘째 줄", "셋째 줄"])
        first = self.one_shot(bad)
        code, client, log = self.run_with([first, msg([text(json.dumps(bad, ensure_ascii=False))])])
        self.assertEqual(code, 2)
        self.assertEqual(len(client.calls), 2)            # 다시 쓰기는 한 번만
        self.assertFalse(self.out.exists())
        self.assertTrue(any("70자" in r for r in log["repaired_because"]))
        self.assertTrue(any("70자" in e for e in log["errors"]))

    def test_no_json_goes_to_repair(self):
        first = msg([text("정리: 나스닥 상승, 비트코인 하락. JSON 은 깜빡함.")])
        good = answer()
        good["schedule"] = [{k: v for k, v in x.items() if k != "src"} for x in good["schedule"]]
        good["schedule"][0]["when"] = "11/18 (수) 22:30"
        code, client, log = self.run_with([first, msg([text(json.dumps(good, ensure_ascii=False))])])
        self.assertEqual(code, 0)
        self.assertIn("JSON 은 깜빡함", client.calls[1]["messages"][0]["content"])
        self.assertEqual(log["repaired_because"], ["답에서 JSON 블록을 찾지 못함"])

    def test_budget_finalize(self):
        paused = msg([text("더 찾음"), stu("s1", query="q1"), wsr("s1", URLS), stu("s2", query="q2")],
                     stop="pause_turn", u=usage(inp=1000, searches=1))
        done = msg([text(fenced(answer()))])
        code, client, log = self.run_with([paused, done], "--max-usd", "0.001")
        self.assertEqual(code, 0)
        self.assertEqual(len(client.calls), 2)
        k = client.calls[1]
        self.assertEqual(k["tool_choice"], {"type": "none"})
        self.assertEqual([m["role"] for m in k["messages"]], ["user", "assistant", "user"])
        self.assertEqual(k["messages"][2]["content"], B.FINALIZE_MSG)
        ids = [b["id"] for b in k["messages"][1]["content"] if b["type"] == "server_tool_use"]
        self.assertEqual(ids, ["s1"])   # 결과 없는 s2 는 뺌
        self.assertTrue(any("예산" in w for w in log["warnings"]))
        self.assertEqual(log["requests"][1]["step"], "finalize")

    def test_request_cap_finalize(self):
        p = lambda: msg([text("이어짐")], stop="pause_turn")
        code, client, log = self.run_with([p(), p(), msg([text(fenced(answer()))])], "--max-requests", "2")
        self.assertEqual(code, 0)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[2]["tool_choice"], {"type": "none"})
        self.assertTrue(any("요청 수 상한" in w for w in log["warnings"]))

    def test_finalize_refused(self):
        p = msg([text("이어짐")], stop="pause_turn")
        code, _, log = self.run_with([p, msg([], stop="refusal")], "--max-requests", "1")
        self.assertEqual(code, 3)
        self.assertIn("마무리", log["errors"][0])

    def test_unexpected_tool_use(self):
        code, _, log = self.run_with([msg([Obj(type="tool_use", id="t", name="x", input={})], stop="tool_use")])
        self.assertEqual(code, 3)
        self.assertIn("tool_use", log["errors"][0])

    def test_exception_keeps_log(self):
        code, _, log = self.run_with([RuntimeError("연결 끊김")])
        self.assertEqual(code, 1)
        self.assertIn("연결 끊김", log["errors"][0])
        self.assertFalse(self.out.exists())

    def test_effort_and_caps_flags(self):
        first, last = self.research_msgs(answer())
        _, client, log = self.run_with([first, last], "--effort", "medium", "--max-searches", "12", "--max-fetches", "3")
        k = client.calls[0]
        self.assertEqual(k["output_config"], {"effort": "medium"})
        self.assertEqual(k["tools"][0]["max_uses"], 12)
        self.assertEqual(k["tools"][1]["max_uses"], 3)
        self.assertEqual(log["effort"], "medium")

    def test_hide_usd(self):
        first, last = self.research_msgs(answer())
        client = FakeClient([first, last])
        with mock.patch("builtins.print") as p:
            code = B.run_brief(client, self.args("--hide-usd"), C, rules=B.RULES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        printed = " ".join(str(x) for call in p.call_args_list for x in call.args)
        self.assertIn("캐시쓰기", printed)
        self.assertNotIn("$", printed.replace("$95,123", "").replace("$3,456", ""))   # 브리핑 본문의 시세만 남음
        raw = self.log.read_text(encoding="utf-8")
        self.assertNotIn('"usd', raw)
        self.assertIn('"max_usd"', raw)          # 상한 설정값은 남김
        self.assertEqual(json.loads(raw)["totals"]["web_search_requests"], 7)


class MiscTest(Base):
    def test_number_check(self):
        brief = {"overnight": ["나스닥 −0.91% · S&P500 −0.40% (11/13 마감)", "비트코인 $95,123 · 10월 PPI"]}
        r = B.number_check(brief, ["Nasdaq fell 0.91% while the S&P 500 lost 0.40%", "BTC 95123"])
        self.assertEqual(r["checked"], 3)
        self.assertEqual(r["not_found"], [])
        r = B.number_check(brief, ["nothing"])
        self.assertEqual(r["not_found"], ["0.91", "0.40", "95,123"])
        r = B.number_check({"overnight": ["나스닥100 +1.20% · 코스피200 −0.4% · 러셀2000 보합"]}, ["up 1.2%, down 0.4%"])
        self.assertEqual((r["checked"], r["not_found"]), (2, []))   # 1.20 ↔ 1.2 · 이름 속 숫자는 안 셈
        r = B.number_check({"overnight": ["비트코인 $95,123"]}, ["95,1234"])
        self.assertEqual(r["not_found"], ["95,123"])                 # 더 긴 숫자의 앞부분은 같은 숫자가 아님

    def test_seen_urls_from_code_output(self):
        s = B.Seen()
        s.add([Obj(type="bash_code_execution_tool_result", tool_use_id="c1",
                   content=Obj(type="bash_code_execution_result", stdout="1. https://d.example/4, 2. https://e.example/5.",
                               stderr="", return_code=0))])
        self.assertIn("https://d.example/4", s.urls)
        self.assertIn("https://e.example/5", s.urls)
        brief, errs, warns = B.normalize(answer(sources=["https://d.example/4", "https://e.example/5", URLS[0]]), C, s.urls)
        self.assertEqual(brief["sources"], ["https://d.example/4", "https://e.example/5"])
        self.assertTrue(any("a.example" in w for w in warns))

    def test_check_file(self):
        live_like = answer()
        live_like.pop("dropped")
        live_like["date"] = "2026-11-16"
        for x in live_like["schedule"]:
            x.pop("src", None)
        live_like["schedule"][0]["when"] = "11/18 (수) 22:30"
        p = self.tmp / "live.json"
        p.write_text(json.dumps(live_like, ensure_ascii=False), encoding="utf-8")
        with mock.patch("builtins.print"):
            self.assertEqual(B.check_file(p, C), 0)

    def test_main_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stderr"):
            self.assertEqual(B.main(["--out", str(self.out), "--log", str(self.log)]), 4)
        self.assertFalse(self.log.exists())

    def test_main_check_mode_needs_no_key(self):
        p = self.tmp / "x.json"
        p.write_text(json.dumps(dict(answer(), date="2026-11-16"), ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("builtins.print"):
            self.assertEqual(B.main(["--check", str(p), "--now", "2026-11-16T06:23+09:00"]), 0)

    def test_repair_schema_strict(self):
        def walk(s):
            if s.get("type") == "object":
                self.assertIs(s.get("additionalProperties"), False)
                self.assertEqual(set(s["required"]), set(s["properties"]))
                for v in s["properties"].values():
                    walk(v)
            if s.get("type") == "array":
                walk(s["items"])
            for bad in ("minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "pattern"):
                self.assertNotIn(bad, s)
        walk(B.REPAIR_SCHEMA)

    def test_rules_sections(self):
        rules = B.RULES_FILE.read_text(encoding="utf-8")
        self.assertIn("## 조사", rules)
        self.assertIn("## 답 모양", rules)
        rs = B.repair_system(rules)
        self.assertNotIn("찾을 것", rs)
        self.assertIn("overnight: 3~6줄", rs)


if __name__ == "__main__":
    unittest.main()
