#!/usr/bin/env python3
"""캔들맨 아침 경제 브리핑 — Claude API 판.

순서
  1. 조사: Claude(claude-opus-5-5)가 웹 검색 · 웹 가져오기로 밤사이 시장과 2주 일정을 찾아 JSON 초안을 낸다.
     pause_turn 이면 이어 보내고, 예산(요청 수 · 검색 수 · 달러)을 넘으면 더 찾지 말고 마무리하게 한다.
  2. 정리: 프로그램이 날짜 · 한국 시각 · 요일 · 순서 · 개수 · 출처를 맞추고 규칙을 검사한다.
  3. 다시 쓰기: 고칠 것이 남으면 도구 없이 한 번 고쳐 쓰게 한다(구조화 출력). 그래도 남으면 쓰지 않는다.
  4. 쓰기: --out 에 브리핑 JSON, --log 에 실행 기록(사용량 · 경고 · 오류)을 쓴다. git 은 워크플로가 한다.

API 키는 SDK 가 환경 변수 ANTHROPIC_API_KEY 에서 읽는다. 이 프로그램은 키 값을 찍지 않는다.
종료 코드: 0 성공 · 1 예상 못 한 오류 · 2 검사 불합격 · 3 API 가 멈춤(거절 · 출력 한도 · 예산) · 4 키 없음.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
RULES_FILE = HERE / "rules.md"

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]   # fallbacks="default": 안전 분류기가 거절하면 서버가 권장 모델로 다시 돌림
WD = "월화수목금토일"

# Opus 5.5 단가($ / 100만 토큰)와 웹 검색 단가($ / 1회). 대체 모델이 답한 몫은 실제와 다를 수 있다(기록용 어림).
PRICE_IN, PRICE_OUT, PRICE_CACHE_W, PRICE_CACHE_R = 4.0, 20.0, 5.0, 0.20
PRICE_SEARCH = 10.0 / 1000

FETCH_BLOCKED = ["investing.com", "tradingview.com", "stooq.com", "naver.com"]   # 자동 수집을 하지 않는 곳
OFFICIAL = ["https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            "https://www.bea.gov/news/schedule"]

N_OVERNIGHT = (3, 6)
LINE_MAX = 60
N_SCHEDULE = (3, 7)
WHY_MAX = 30
WHAT_MAX = 60
N_SOURCES = (3, 10)
AHEAD_DAYS = 14

FINALIZE_MSG = ("조사 예산이 다 됐다. 더 찾지 말고 지금까지 확인한 것만으로 답 모양의 ```json 블록 하나를 낸다. "
                "두 출처로 확인하지 못한 숫자는 뺀다.")


class Stop(Exception):
    """API 가 답을 끝내지 못함(거절 · 출력 한도 · 예상 밖 멈춤 · 예산)."""


# ── 날짜 · 시각 ─────────────────────────────────────────────

def wd(d):
    return WD[d.weekday()]


def context(now):
    """now(시간대가 있는 datetime) → 지시문 · 검사에 쓰는 날짜 사실."""
    k, e = now.astimezone(KST), now.astimezone(ET)
    today = k.date()
    dst = bool(e.dst())
    last = e.date() if e.hour >= 16 else e.date() - dt.timedelta(days=1)   # 끝난 미국 정규장 중 가장 최근 날(미국 날짜)
    if last.weekday() >= 5:
        fri = last - dt.timedelta(days=last.weekday() - 4)
        session = ("없음 — 미국 날짜 %s (%s)은 주말. 마지막 거래일은 %s (%s)."
                   % (last.isoformat(), wd(last), fri.isoformat(), wd(fri)))
    else:
        session = ("미국 날짜 %s (%s) · 마감은 한국 시각 %s. 미국 공휴일로 쉬었으면 휴장이라고 적는다."
                   % (last.isoformat(), wd(last), "05:00" if dst else "06:00"))
    return {
        "now": k, "today": today, "tomorrow": today + dt.timedelta(days=1),
        "end": today + dt.timedelta(days=AHEAD_DAYS),
        "et": e, "dst": dst, "close_kst": "05:00" if dst else "06:00", "session": session,
    }


def user_message(c):
    k, e = c["now"], c["et"]
    return "\n".join([
        "오늘은 한국 시간 %s (%s), 지금 %s KST다. 미국 동부는 %s (%s) %s · %s이고, 미국 장 마감은 한국 시각 %s다."
        % (c["today"].isoformat(), wd(c["today"]), k.strftime("%H:%M"), e.date().isoformat(), wd(e.date()),
           e.strftime("%H:%M"), "서머타임(UTC−4)" if c["dst"] else "표준시(UTC−5)", c["close_kst"]),
        "지난밤 미국 정규장: " + c["session"],
        "'오늘 · 내일'은 한국 날짜 %s · %s다. 일정은 지금부터 %s(한국 날짜)까지만 넣는다."
        % (c["today"].isoformat(), c["tomorrow"].isoformat(), c["end"].isoformat()),
        "공식 일정 주소(가져와 봐도 됨): " + " · ".join(OFFICIAL),
        "지시대로 조사한 뒤, 마지막에 ```json 블록 하나로 답한다.",
    ])


WHEN_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*(?:\(\s*(\S)\s*\))?\s*"
                     r"(?:(\d{1,2}):(\d{2})|(\d{1,2})\s*시\s*쯤|(새벽|아침|오전|낮|오후|저녁|밤))?\s*$")
LABEL_MIN = {"새벽": 4 * 60, "아침": 7 * 60, "오전": 9 * 60, "낮": 12 * 60, "오후": 15 * 60, "저녁": 19 * 60, "밤": 21 * 60}


def nearest_date(month, day, today):
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def parse_when(s, today):
    """'MM/DD (요일) HH:MM' 등 → dict(date, kind, hh, mm, label, wd_given). 못 읽으면 None."""
    m = WHEN_RE.match(str(s or ""))
    if not m:
        return None
    mo, da, wdg, hh, mi, hh2, label = m.groups()
    d = nearest_date(int(mo), int(da), today)
    if d is None:
        return None
    if hh is not None:
        if int(hh) > 23 or int(mi) > 59:
            return None
        return {"date": d, "kind": "exact", "hh": int(hh), "mm": int(mi), "label": None, "wd_given": wdg}
    if hh2 is not None:
        if int(hh2) > 23:
            return None
        return {"date": d, "kind": "approx", "hh": int(hh2), "mm": 0, "label": None, "wd_given": wdg}
    if label:
        return {"date": d, "kind": "label", "hh": None, "mm": None, "label": label, "wd_given": wdg}
    return {"date": d, "kind": "date", "hh": None, "mm": None, "label": None, "wd_given": wdg}


def format_when(p):
    d = p["date"]
    head = "%02d/%02d (%s)" % (d.month, d.day, wd(d))
    if p["kind"] == "exact":
        return "%s %02d:%02d" % (head, p["hh"], p["mm"])
    if p["kind"] == "approx":
        return "%s %02d시쯤" % (head, p["hh"])
    if p["kind"] == "label":
        return "%s %s" % (head, p["label"])
    return head


def when_minutes(p):
    if p["kind"] in ("exact", "approx"):
        return p["hh"] * 60 + p["mm"]
    if p["kind"] == "label":
        return LABEL_MIN.get(p["label"], 0)
    return 24 * 60   # 시각 모름은 그날 맨 뒤(지금 판과 같은 순서)


def from_src(src):
    """src {tz, date, time, approx} → parse_when 과 같은 모양(한국 시각). 못 읽으면 ValueError."""
    tz = ZoneInfo(str(src["tz"]))
    d = dt.date.fromisoformat(str(src["date"]))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(src["time"]).strip())
    if not m:
        raise ValueError("time 모양")
    hh, mi = int(m.group(1)), int(m.group(2))
    if hh > 23 or mi > 59:
        raise ValueError("time 범위")
    k = dt.datetime(d.year, d.month, d.day, hh, mi, tzinfo=tz).astimezone(KST)
    if src.get("approx") is True:
        return {"date": k.date(), "kind": "approx", "hh": k.hour, "mm": 0, "label": None, "wd_given": None}
    return {"date": k.date(), "kind": "exact", "hh": k.hour, "mm": k.minute, "label": None, "wd_given": None}


# ── 검사 · 정리 ─────────────────────────────────────────────

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
FORBID = ("**", "__", "`", "http://", "https://", "www.", "@", "](")
SIGNED_PCT_RE = re.compile(r"([+\-−±])\s?(\d+(?:\.\d+)?)\s?%")


def clean(s):
    return " ".join(str(s).split()) if s is not None else ""


def text_problems(s, where):
    out = []
    if EMOJI_RE.search(s):
        out.append("%s: 이모지 · 기호를 빼야 함 — %s" % (where, s))
    for f in FORBID:
        if f in s:
            out.append("%s: '%s' 를 빼야 함(굵게 · 링크 · @ 금지) — %s" % (where, f, s))
            break
    for sign, num in SIGNED_PCT_RE.findall(s):
        if float(num) < 0.05:
            out.append("%s: %s%s%% 는 '보합'으로 써야 함 — %s" % (where, sign, num, s))
    return out


def url_key(u):
    u = u.strip().split("#", 1)[0].rstrip("/")
    m = re.match(r"(https?)://([^/]+)(.*)$", u, re.I)
    return (m.group(1).lower() + "://" + m.group(2).lower() + m.group(3)) if m else u


def normalize(obj, c, seen_urls=None):
    """모델이 낸 dict → (브리핑, 오류, 경고). 오류가 있으면 쓰지 않고 다시 쓰기로 넘긴다."""
    errors, warnings = [], []
    if not isinstance(obj, dict):
        return None, ["JSON 이 객체(dict)가 아님"], warnings
    today = c["today"]

    # overnight
    raw = obj.get("overnight")
    night = []
    if not isinstance(raw, list):
        errors.append("overnight 가 목록이 아님")
    else:
        night = [clean(x) for x in raw if isinstance(x, str) and clean(x)]
        if len(night) != len([x for x in raw if x is not None]):
            warnings.append("overnight 에 글이 아닌 것 · 빈 줄이 있어 뺌")
    if not night and isinstance(raw, list):
        errors.append("overnight 가 비어 있음")
    if len(night) > N_OVERNIGHT[1]:
        errors.append("overnight 가 %d줄 (%d줄까지)" % (len(night), N_OVERNIGHT[1]))
    elif 0 < len(night) < N_OVERNIGHT[0]:
        warnings.append("overnight 가 %d줄 (%d줄 이상 권장)" % (len(night), N_OVERNIGHT[0]))
    for i, s in enumerate(night, 1):
        if len(s) > LINE_MAX:
            errors.append("overnight %d번째 줄이 %d자 (%d자까지): %s" % (i, len(s), LINE_MAX, s))
        errors.extend(text_problems(s, "overnight %d번째 줄" % i))

    # schedule
    raw = obj.get("schedule")
    items = []
    if not isinstance(raw, list):
        errors.append("schedule 이 목록이 아님")
        raw = []
    now_min = c["now"].hour * 60 + c["now"].minute
    for i, x in enumerate(raw, 1):
        where = "일정 %d" % i
        if not isinstance(x, dict):
            errors.append("%s: 객체가 아님 — %r" % (where, x))
            continue
        what, why = clean(x.get("what")), clean(x.get("why"))
        imp = x.get("impact")
        if isinstance(imp, str) and imp.strip().isdigit():
            imp = int(imp.strip())
        if isinstance(imp, bool) or not isinstance(imp, int) or not 1 <= imp <= 5:
            errors.append("%s: impact 는 1~5 정수여야 함 — %r" % (where, x.get("impact")))
        if not what:
            errors.append("%s: what 이 비어 있음" % where)
        elif len(what) > WHAT_MAX:
            warnings.append("%s: what 이 %d자 (%d자 넘음)" % (where, len(what), WHAT_MAX))
        if not why:
            errors.append("%s: why 가 비어 있음" % where)
        elif len(why) > WHY_MAX:
            errors.append("%s: why 가 %d자 (%d자까지): %s" % (where, len(why), WHY_MAX, why))
        errors.extend(text_problems(what, where + " what"))
        errors.extend(text_problems(why, where + " why"))

        when_raw = clean(x.get("when"))
        p = None
        src = x.get("src")
        if isinstance(src, dict) and src.get("time") not in (None, ""):
            try:
                p = from_src(src)
            except (KeyError, ValueError, TypeError) as e:   # ZoneInfoNotFoundError 는 KeyError 갈래
                warnings.append("%s: src 를 못 읽어 when 을 그대로 씀 — %r (%s)" % (where, src, type(e).__name__))
        if p is not None:
            new = format_when(p)
            old = parse_when(when_raw, today)
            if when_raw and (old is None or format_when(old) != new):
                warnings.append("%s: when 을 src 로 다시 셈 — '%s' → '%s'" % (where, when_raw, new))
        else:
            p = parse_when(when_raw, today)
            if p is None:
                errors.append("%s: when 모양이 틀림 — '%s'" % (where, when_raw))
                items.append({"when": when_raw, "what": what, "impact": imp, "why": why, "_p": None})
                continue
            if p["wd_given"] and p["wd_given"] != wd(p["date"]):
                warnings.append("%s: 요일을 고침 — '%s' → '%s'" % (where, when_raw, format_when(p)))
        d = p["date"]
        if d < today or (d == today and p["kind"] in ("exact", "approx") and when_minutes(p) < now_min):
            warnings.append("%s: 이미 지난 일정이라 뺌 — %s %s" % (where, format_when(p), what))
            continue
        if d > c["end"]:
            warnings.append("%s: 2주 밖 일정이라 뺌 — %s %s" % (where, format_when(p), what))
            continue
        items.append({"when": format_when(p), "what": what, "impact": imp, "why": why, "_p": p})

    def key(it):
        p = it["_p"]
        return (p["date"], when_minutes(p)) if p else (dt.date.max, 0)

    items.sort(key=key)
    if len(items) > N_SCHEDULE[1] and all(it["_p"] and isinstance(it["impact"], int) for it in items):
        items, cut = trim(items, c)
        for it in cut:
            warnings.append("일정이 %d개를 넘어 뺌 — %s %s (%s점)" % (N_SCHEDULE[1], it["when"], it["what"], it["impact"]))
    elif len(items) > N_SCHEDULE[1]:
        errors.append("일정이 %d개 (%d개까지)" % (len(items), N_SCHEDULE[1]))
    if len(items) < N_SCHEDULE[0]:
        warnings.append("일정이 %d개 (%d개 이상 권장)" % (len(items), N_SCHEDULE[0]))

    # sources
    raw = obj.get("sources")
    srcs = []
    for u in raw if isinstance(raw, list) else []:
        u = clean(u)
        if re.fullmatch(r"https?://\S+", u) and u not in srcs:
            srcs.append(u)
    if seen_urls:
        keys = {url_key(u) for u in seen_urls}
        unseen = [u for u in srcs if url_key(u) not in keys]
        if unseen:
            warnings.append("검색 · 가져오기 결과에 없던 출처 %d개를 뺌: %s" % (len(unseen), " · ".join(unseen)))
            srcs = [u for u in srcs if u not in unseen]
    if len(srcs) > N_SOURCES[1]:
        warnings.append("출처가 %d개라 앞 %d개만 둠" % (len(srcs), N_SOURCES[1]))
        srcs = srcs[:N_SOURCES[1]]
    if len(srcs) < N_SOURCES[0]:
        warnings.append("출처가 %d개 (%d개 이상 권장)" % (len(srcs), N_SOURCES[0]))

    brief = {
        "date": today.isoformat(),
        "overnight": night,
        "schedule": [{"when": it["when"], "what": it["what"], "impact": it["impact"], "why": it["why"]} for it in items],
        "sources": srcs,
    }
    return brief, errors, warnings


def trim(items, c):
    """7개를 넘으면: 오늘 · 내일의 2점 이상은 지키고, 나머지에서 점수 낮은 것 · 늦은 날짜부터 뺀다."""
    cap = N_SCHEDULE[1]
    keep = {i for i, it in enumerate(items) if it["_p"]["date"] in (c["today"], c["tomorrow"]) and it["impact"] >= 2}
    rest = sorted((i for i in range(len(items)) if i not in keep),
                  key=lambda i: (items[i]["impact"], -items[i]["_p"]["date"].toordinal(), -i))
    drop = set()
    while len(items) - len(drop) > cap and rest:
        drop.add(rest.pop(0))
    left = [i for i in range(len(items)) if i not in drop]
    if len(left) > cap:   # 오늘 · 내일 것만으로도 넘치면 늦은 것부터
        drop.update(left[cap:])
        left = left[:cap]
    return [items[i] for i in left], [items[i] for i in sorted(drop)]


# ── 응답 읽기 ───────────────────────────────────────────────

def to_dict(o):
    if o is None:
        return None
    if isinstance(o, dict):
        return o
    for name in ("to_dict", "model_dump"):
        f = getattr(o, name, None)
        if callable(f):
            return f()
    return dict(vars(o))


SKIP_KEYS = {"encrypted_content", "encrypted_index", "signature", "id", "tool_use_id", "caller"}
URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]{}|\\^`]+")


def strings(o, key=None, pdf=False):
    """블록 안의 글(키, 값)을 모두 꺼냄. 암호문 · PDF 본문(base64)은 건너뜀."""
    if isinstance(o, dict):
        pdf = pdf or "pdf" in str(o.get("media_type") or "")
        for k, v in o.items():
            if k in SKIP_KEYS or (k == "data" and pdf):
                continue
            yield from strings(v, k, pdf)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v, key, pdf)
    elif isinstance(o, str):
        yield key, o


class Seen:
    """조사 중 나온 주소 · 검색어 · 근거 글(숫자 대조용) · 도구 오류."""

    def __init__(self):
        self.urls, self.evidence, self.queries, self.fetched, self.tool_errors = set(), [], [], [], []

    def add(self, content):
        for b in content or []:
            d = to_dict(b) or {}
            t = str(d.get("type") or "")
            if t == "server_tool_use":
                inp = d.get("input") if isinstance(d.get("input"), dict) else {}
                if d.get("name") == "web_search":
                    self.queries.append(clean(inp.get("query"))[:200])
                elif d.get("name") == "web_fetch":
                    self.fetched.append(clean(inp.get("url"))[:300])
            elif t.endswith("_tool_result"):
                cont = d.get("content")
                if isinstance(cont, dict) and str(cont.get("type") or "").endswith("_error"):
                    self.tool_errors.append("%s:%s" % (t, cont.get("error_code")))
                for k, s in strings(d):
                    if k == "url":
                        self.urls.add(s)
                    elif len(s) > 3:
                        self.evidence.append(s)
                        for u in URL_RE.findall(s):   # 동적 필터링(코드 실행) 출력 속 주소도 '나온 주소'
                            self.urls.add(u.rstrip(".,;:"))
            elif t == "text":
                for ci in d.get("citations") or []:
                    ci = to_dict(ci) or {}
                    if ci.get("url"):
                        self.urls.add(ci["url"])
                    if ci.get("cited_text"):
                        self.evidence.append(ci["cited_text"])


def final_text(content):
    """(마지막 도구 블록 뒤의 글, 응답의 모든 글)."""
    ds = [to_dict(b) or {} for b in content or []]
    plain = ("text", "thinking", "redacted_thinking", "fallback")
    last_tool = max((i for i, d in enumerate(ds) if str(d.get("type") or "") not in plain), default=-1)
    tail = "".join(d.get("text") or "" for d in ds[last_tool + 1:] if d.get("type") == "text")
    whole = "".join(d.get("text") or "" for d in ds if d.get("type") == "text")
    return tail, whole


FENCE_RE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.S)


def extract_json(text):
    """글에서 브리핑 JSON(dict)을 찾음: ```json 블록 중 마지막으로 읽히는 것, 없으면 맨바닥의 마지막 객체."""
    text = text or ""
    for m in reversed(list(FENCE_RE.finditer(text))):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    dec, found, i = json.JSONDecoder(), None, 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return found
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and ("overnight" in obj or "schedule" in obj):
            found = obj
        i = end


def echo_blocks(content):
    """pause_turn 뒤 되돌려 보낼 블록. 대체 모델로 넘어간 경계(fallback 블록)가 있으면 문서대로
    그 앞의 thinking · tool_use · 짝 없는 server_tool_use · 모르는 블록을 뺀다. 그 밖엔 그대로."""
    content = list(content or [])
    ds = [to_dict(b) or {} for b in content]
    last_fb = max((i for i, d in enumerate(ds) if d.get("type") == "fallback"), default=None)
    if last_fb is None:
        return content
    res_ids = {d.get("tool_use_id") for d in ds if str(d.get("type") or "").endswith("_tool_result")}
    use_ids = {d.get("id") for d in ds if d.get("type") == "server_tool_use"}
    paired = res_ids & use_ids
    out = []
    for i, (b, d) in enumerate(zip(content, ds)):
        t = str(d.get("type") or "")
        if i >= last_fb or t == "text" \
                or (t == "server_tool_use" and d.get("id") in paired) \
                or (t.endswith("_tool_result") and d.get("tool_use_id") in paired):
            out.append(b)
    return out


def drop_unpaired(messages):
    """마무리 전: 마지막 assistant 메시지 끝의 '결과 없는 server_tool_use'(멈춘 자리)와 그 뒤에 남는 끝 thinking 을 뺀다.
    끝에서만 빼므로 앞 thinking 블록의 서명(대화 앞부분과 묶임)은 그대로 유효하다. 다 비면 그 메시지를 뺀다."""
    last = messages[-1] if messages else None
    if not last or last["role"] != "assistant":
        return list(messages)
    res_ids = set()
    for m in messages:
        if m["role"] == "assistant":
            for b in m["content"]:
                d = to_dict(b) or {}
                if str(d.get("type") or "").endswith("_tool_result"):
                    res_ids.add(d.get("tool_use_id"))
    keep = [b for b in last["content"]
            if not ((to_dict(b) or {}).get("type") == "server_tool_use" and (to_dict(b) or {}).get("id") not in res_ids)]
    while keep and (to_dict(keep[-1]) or {}).get("type") in ("thinking", "redacted_thinking"):
        keep.pop()
    return messages[:-1] + ([{"role": "assistant", "content": keep}] if keep else [])


# ── 사용량 · 비용 ───────────────────────────────────────────

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def usd_of(u):
    return (u.get("input_tokens", 0) * PRICE_IN + u.get("output_tokens", 0) * PRICE_OUT
            + u.get("cache_creation_input_tokens", 0) * PRICE_CACHE_W
            + u.get("cache_read_input_tokens", 0) * PRICE_CACHE_R) / 1e6 \
        + u.get("web_search_requests", 0) * PRICE_SEARCH


def iteration_tokens(its):
    """usage.iterations 를 종류(message · fallback_message · compaction …)별 횟수 · 토큰 합으로 묶는다.
    달러가 아니라 토큰만이라 공개 로그에도 남긴다 — 맨 위 합계와 견주는 데 쓴다."""
    by = {}
    for x in its:
        b = by.setdefault(str(x.get("type") or "unknown"), dict.fromkeys(("n",) + TOKEN_KEYS, 0))
        b["n"] += 1
        for k in TOKEN_KEYS:
            b[k] += int(x.get(k) or 0)
    return by


def usage_of(msg):
    u = to_dict(getattr(msg, "usage", None)) or {}
    st = to_dict(u.get("server_tool_use")) or {}
    rec = {k: int(u.get(k) or 0) for k in TOKEN_KEYS}
    rec["web_search_requests"] = int(st.get("web_search_requests") or 0)
    rec["web_fetch_requests"] = int(st.get("web_fetch_requests") or 0)
    its = [to_dict(x) or {} for x in (u.get("iterations") or [])]
    rec["iterations"] = len(its)
    rec["fallback_ran"] = any(x.get("type") == "fallback_message" for x in its)
    by = iteration_tokens(its)
    if by:
        rec["iteration_tokens"] = by
    # compaction 반복의 토큰은 맨 위 usage 에 안 들어가므로(SDK 설명) 어림에 따로 더한다
    rec["usd"] = round(usd_of(rec) + usd_of(by.get("compaction", {})), 4)
    if len(its) > 1:   # 반복이 여럿인 요청: 반복별 합으로도 셈해 맨 위 합계 · 콘솔 청구와 견줌
        alt = {k: sum(b[k] for b in by.values()) for k in TOKEN_KEYS}
        rec["usd_iterations_sum"] = round(usd_of(dict(alt, web_search_requests=rec["web_search_requests"])), 4)
    return rec


class Run:
    def __init__(self, args, c):
        self.args, self.c = args, c
        self.hide = bool(getattr(args, "hide_usd", False))   # 공개 로그(깃허브 액션)엔 달러 어림을 안 남김
        self.t0 = time.time()
        self.started = dt.datetime.now(dt.timezone.utc)
        self.requests, self.warnings, self.errors, self.repair_reasons = [], [], [], []
        self.seen = Seen()
        self.dropped, self.numbers, self.brief, self.models = None, None, None, set()

    def note(self, msg, step, sec):
        rec = usage_of(msg)
        rec.update(step=step, stop=getattr(msg, "stop_reason", None), model=getattr(msg, "model", None), sec=round(sec, 1))
        sd = to_dict(getattr(msg, "stop_details", None))
        if sd:
            rec["stop_details"] = {k: sd.get(k) for k in ("type", "category", "explanation") if sd.get(k)}
        self.requests.append(rec)
        self.models.add(rec["model"])
        self.seen.add(getattr(msg, "content", None))
        cost = "" if self.hide else " · $%.3f (누적 $%.3f)" % (rec["usd"], self.usd())
        print("[%s %d] stop=%s 검색 %d · 가져오기 %d · 입력 %d · 캐시쓰기 %d · 캐시읽기 %d · 출력 %d · %.0f초%s"
              % (step, len(self.requests), rec["stop"], rec["web_search_requests"], rec["web_fetch_requests"],
                 rec["input_tokens"], rec["cache_creation_input_tokens"], rec["cache_read_input_tokens"],
                 rec["output_tokens"], sec, cost),
              flush=True)

    def usd(self):
        return sum(r["usd"] for r in self.requests)

    def searches(self):
        return sum(r["web_search_requests"] for r in self.requests)

    def totals(self):
        keys = TOKEN_KEYS + ("web_search_requests", "web_fetch_requests")
        t = {k: sum(r[k] for r in self.requests) for k in keys}
        t["usd"] = round(self.usd(), 4)
        t["requests"] = len(self.requests)
        it = {}
        for r in self.requests:
            for typ, b in (r.get("iteration_tokens") or {}).items():
                acc = it.setdefault(typ, dict.fromkeys(b, 0))
                for k, v in b.items():
                    acc[k] += v
        if it:
            t["iteration_tokens"] = it
        return t

    def record(self, code):
        a = self.args
        reqs, totals = self.requests, self.totals()
        if self.hide:
            reqs = [{k: v for k, v in r.items() if not k.startswith("usd")} for r in reqs]
            totals.pop("usd", None)
        return {
            "date": self.c["today"].isoformat(),
            "started_utc": self.started.isoformat(timespec="seconds"),
            "seconds": round(time.time() - self.t0, 1),
            "code": code,
            "model": MODEL, "served_by": sorted(m for m in self.models if m),
            "effort": a.effort,
            "caps": {"max_searches_per_request": a.max_searches, "max_fetches_per_request": a.max_fetches,
                     "max_requests": a.max_requests, "max_total_searches": a.max_total_searches, "max_usd": a.max_usd},
            "totals": totals,
            "requests": reqs,
            "queries": self.seen.queries,
            "fetched": self.seen.fetched,
            "tool_errors": self.seen.tool_errors,
            "repaired_because": self.repair_reasons,
            "warnings": self.warnings,
            "errors": self.errors,
            "dropped": self.dropped,
            "numbers": self.numbers,
            "brief": self.brief,
        }


# ── 요청 ────────────────────────────────────────────────────

def tools(max_searches, max_fetches):
    return [
        {"type": "web_search_20260318", "name": "web_search", "max_uses": max_searches,
         "user_location": {"type": "approximate", "city": "Seoul", "region": "Seoul",
                           "country": "KR", "timezone": "Asia/Seoul"}},
        {"type": "web_fetch_20260318", "name": "web_fetch", "max_uses": max_fetches,
         "blocked_domains": FETCH_BLOCKED, "citations": {"enabled": True},
         "max_content_tokens": 10000, "use_cache": False},
    ]


def ask(client, *, messages, system, effort, max_tokens, tools_=None, tool_choice=None, fmt=None, cache=False):
    kw = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": list(messages),
          "betas": BETAS, "fallbacks": "default", "output_config": {"effort": effort}}
    if fmt:
        kw["output_config"]["format"] = fmt
    if tools_:
        kw["tools"] = tools_
    if tool_choice:
        kw["tool_choice"] = tool_choice
    if cache:
        kw["cache_control"] = {"type": "ephemeral"}
    with client.beta.messages.stream(**kw) as stream:
        return stream.get_final_message()


def stop_text(msg, step):
    sr = getattr(msg, "stop_reason", None)
    if sr == "refusal":
        sd = to_dict(getattr(msg, "stop_details", None)) or {}
        return "%s: 거절(refusal · %s)" % (step, sd.get("category"))
    if sr == "max_tokens":
        return "%s: 출력 한도(max_tokens)에 걸림" % step
    return "%s: 예상 밖 멈춤(stop_reason=%s)" % (step, sr)


def research(client, rules, run):
    a = run.args
    msgs = [{"role": "user", "content": user_message(run.c)}]
    tl = tools(a.max_searches, a.max_fetches)
    for _ in range(a.max_requests):
        t = time.time()
        msg = ask(client, messages=msgs, system=rules, effort=a.effort, max_tokens=a.max_tokens, tools_=tl, cache=True)
        run.note(msg, "research", time.time() - t)
        sr = msg.stop_reason
        if sr == "end_turn":
            return msg
        if sr != "pause_turn":
            raise Stop(stop_text(msg, "조사"))
        msgs.append({"role": "assistant", "content": echo_blocks(msg.content)})
        if run.usd() >= a.max_usd or run.searches() >= a.max_total_searches:
            run.warnings.append("예산에 닿아 마무리시킴 (%s검색 %d번)"
                                % ("" if run.hide else "누적 $%.2f · " % run.usd(), run.searches()))
            return finalize(client, rules, msgs, tl, run)
    run.warnings.append("요청 수 상한(%d)에 닿아 마무리시킴" % a.max_requests)
    return finalize(client, rules, msgs, tl, run)


def finalize(client, rules, msgs, tl, run):
    a = run.args
    msgs = drop_unpaired(msgs) + [{"role": "user", "content": FINALIZE_MSG}]
    t = time.time()
    msg = ask(client, messages=msgs, system=rules, effort=a.effort, max_tokens=a.max_tokens, tools_=tl,
              tool_choice={"type": "none"}, cache=True)
    run.note(msg, "finalize", time.time() - t)
    if msg.stop_reason != "end_turn":
        raise Stop(stop_text(msg, "마무리"))
    return msg


REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "overnight": {"type": "array", "items": {"type": "string"}},
        "schedule": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "when": {"type": "string"},
                "what": {"type": "string"},
                "impact": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                "why": {"type": "string"},
            },
            "required": ["when", "what", "impact", "why"],
            "additionalProperties": False,
        }},
        "sources": {"type": "array", "items": {"type": "string"}},
        "dropped": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overnight", "schedule", "sources", "dropped"],
    "additionalProperties": False,
}


def repair_system(rules):
    head = rules.split("## 조사", 1)[0].strip()
    tail = rules.split("## 답 모양", 1)[-1].strip()
    return (head + "\n\n지금 할 일은 조사가 아니라, 이미 만든 초안을 답 모양에 맞게 고쳐 쓰는 것이다. "
            "도구는 없다. src 는 이미 when 에 반영됐으니 쓰지 않는다.\n\n## 답 모양\n" + tail)


def repair(client, rules, draft, problems, run):
    body = draft if isinstance(draft, str) else json.dumps(draft, ensure_ascii=False, indent=1)
    msg_text = ("아래 초안을 답 모양에 맞게 고쳐 JSON 하나로만 답한다. 새 사실 · 숫자 · 일정을 더하지 않는다. "
                "고칠 수 없는 줄이나 일정은 뺀다. 고칠 것에 없는 when · impact 는 그대로 둔다.\n\n고칠 것:\n"
                + "\n".join("- " + p for p in problems) + "\n\n초안:\n" + body[:40000])
    t = time.time()
    msg = ask(client, messages=[{"role": "user", "content": msg_text}], system=repair_system(rules),
              effort="medium", max_tokens=16000, fmt={"type": "json_schema", "schema": REPAIR_SCHEMA})
    run.note(msg, "repair", time.time() - t)
    if msg.stop_reason != "end_turn":
        raise Stop(stop_text(msg, "다시 쓰기"))
    _, whole = final_text(msg.content)
    try:
        return json.loads(whole)
    except ValueError:
        return extract_json(whole)


NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
NAME_NUM_RE = re.compile(r"S&P\s*500|(?<=[A-Za-z가-힣])\d+")   # S&P500 · 나스닥100 · 코스피200 같은 이름 속 숫자


def number_check(brief, evidence):
    """overnight 의 숫자가 조사 중 본 글(인용 · 가져온 본문 · 코드 출력)에 그대로 있는지 — 기록용 어림 검사."""
    blob = re.sub(r"(?<=\d),(?=\d)", "", "\n".join(evidence))
    checked, missing = 0, []
    for line in brief.get("overnight") or []:
        line = re.sub(r"\b\d{1,2}/\d{1,2}\b", " ", line)          # MM/DD 날짜는 뺌
        line = NAME_NUM_RE.sub(" ", line)                          # 지수 이름 속 숫자는 뺌
        for tok in NUM_RE.findall(line):
            n = tok.replace(",", "")
            if len(n.replace(".", "")) < 3 and "." not in n:      # 두 자리 이하 정수(개수 · 순위)는 뺌
                continue
            checked += 1
            alts = {n}
            if "." in n:                                           # 0.40 ↔ 0.4
                a, b = n.split(".", 1)
                alts.add(a + "." + (b.rstrip("0") or b[:1]))
            if not any(re.search(r"(?<![\d.])" + re.escape(x) + r"(?![\d])", blob) for x in alts):
                missing.append(tok)
    return {"checked": checked, "not_found": missing, "evidence_chars": len(blob)}


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_brief(client, args, c, rules=None):
    rules = rules if rules is not None else RULES_FILE.read_text(encoding="utf-8")
    run = Run(args, c)
    code = 1
    try:
        msg = research(client, rules, run)
        tail, whole = final_text(msg.content)
        obj = extract_json(tail) or extract_json(whole)
        if obj is None:
            brief, errs, warns = None, ["답에서 JSON 블록을 찾지 못함"], []
            draft = whole
        else:
            run.dropped = obj.get("dropped")
            brief, errs, warns = normalize(obj, c, run.seen.urls)
            draft = brief if brief is not None else obj
        run.warnings.extend(warns)
        if errs:
            run.repair_reasons = errs
            obj2 = repair(client, rules, draft, errs, run)
            if isinstance(obj2, dict) and obj2.get("dropped") is not None and run.dropped is None:
                run.dropped = obj2.get("dropped")
            brief, errs, warns = normalize(obj2, c, run.seen.urls)
            run.warnings.extend("다시 쓴 뒤: " + w for w in warns)
        if errs:
            run.errors.extend(errs)
            code = 2
        else:
            run.brief = brief
            run.numbers = number_check(brief, run.seen.evidence)
            write_json(args.out, brief)
            code = 0
    except Stop as e:
        run.errors.append(str(e))
        code = 3
    except Exception as e:   # 기록은 남기고 실패로 끝냄
        run.errors.append("%s: %s" % (type(e).__name__, str(e)[:500]))
        traceback.print_exc()
        code = 1
    finally:
        rec = run.record(code)
        if args.log:
            write_json(args.log, rec)
        t = rec["totals"]
        print("끝: 코드 %d · 요청 %d · 검색 %d · 가져오기 %d · 입력 %d · 캐시쓰기 %d · 캐시읽기 %d · 출력 %d%s · %.0f초"
              % (code, t["requests"], t["web_search_requests"], t["web_fetch_requests"], t["input_tokens"],
                 t["cache_creation_input_tokens"], t["cache_read_input_tokens"], t["output_tokens"],
                 "" if "usd" not in t else " · 어림 $%.3f" % t["usd"], rec["seconds"]))
        for w in run.warnings:
            print("경고:", w)
        for e in run.errors:
            print("오류:", e)
        if run.brief:
            print(json.dumps(run.brief, ensure_ascii=False, indent=2))
    return code


def make_client():
    import anthropic   # 실제로 부를 때만 필요(시험 · --check 는 SDK 없이 돎)
    return anthropic.Anthropic(max_retries=4, timeout=anthropic.Timeout(900.0, connect=30.0))


def check_file(path, c):
    """이미 있는 브리핑 JSON 을 같은 검사로 봄(API 안 부름) — 지금 판과 견줄 때."""
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    brief, errs, warns = normalize(obj, c, None)
    print(json.dumps({"errors": errs, "warnings": warns, "brief": brief}, ensure_ascii=False, indent=2))
    return 2 if errs else 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="캔들맨 아침 경제 브리핑 (Claude API 판)")
    p.add_argument("--out", default="briefing_api.json", help="브리핑 JSON 을 쓸 곳")
    p.add_argument("--log", default="run_log.json", help="실행 기록을 쓸 곳 (빈 값이면 안 씀)")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-tokens", type=int, default=64000, help="요청 하나의 출력 한도(생각 포함)")
    p.add_argument("--max-searches", type=int, default=30, help="요청 하나에서 웹 검색 상한")
    p.add_argument("--max-fetches", type=int, default=15, help="요청 하나에서 웹 가져오기 상한")
    p.add_argument("--max-requests", type=int, default=8, help="조사 요청 수 상한(pause_turn 이어 보내기 포함)")
    p.add_argument("--max-total-searches", type=int, default=70, help="조사 전체의 웹 검색 상한(넘으면 마무리)")
    p.add_argument("--max-usd", type=float, default=3.0, help="조사 전체의 어림 비용 상한(넘으면 마무리)")
    p.add_argument("--hide-usd", action="store_true", help="화면 · 기록에 달러 어림을 안 남김(공개 저장소 · 공개 로그용)")
    p.add_argument("--now", help="시각을 정해 돌리기(ISO, 시간대 포함) — 시험용")
    p.add_argument("--check", metavar="JSON", help="API 를 부르지 않고 이 파일만 검사")
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise SystemExit("--now 에는 시간대를 붙여야 함 (예: 2026-11-16T06:23+09:00)")
    c = context(now)
    if a.check:
        return check_file(a.check, c)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 가 없어 멈춤", file=sys.stderr)
        return 4
    return run_brief(make_client(), a, c)


if __name__ == "__main__":
    sys.exit(main())
