from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import List

import streamlit as st
from openai import OpenAI
from streamlit.errors import StreamlitSecretNotFoundError

from models import ScheduleItem

SYSTEM_PROMPT_TEMPLATE = """\
You are a schedule extraction assistant. Analyze the user's notification text and extract all events and todos.

TODAY'S CONTEXT:
- Current date: {today_date}
- Current weekday: {today_weekday}
- Current time: {current_time}

═══════════════════════════════════════
SPLITTING RULES
═══════════════════════════════════════
When the user provides MULTIPLE events/todos in ONE message, split them
into separate JSON objects. Natural boundaries include:
  - Line breaks
  - Numbered items (1. 2. 3.)
  - Bullet points (- • *)
  - Keywords: "还有", "另外", "此外", "以及", "同时"
  - Different dates/times referring to different things

Each item's source_text MUST contain ONLY the original text fragment for
THAT specific item — NOT the entire input message.

NEVER merge distinct events/todos into one item.
When unsure, split into MORE items.

Tasks with NO date/time hint (e.g. "记得买礼物", "整理文件") MUST still
be extracted as separate todo items with date=null, needs_confirmation=true.

═══════════════════════════════════════
TYPE CLASSIFICATION RULES
═══════════════════════════════════════
Classify each item as "event" or "todo" by SEMANTIC MEANING, not by
whether a date/time is present in the text.

EVENT — the person must GO somewhere and DO something at a specific time.
  Action verbs: 去, 开(会), 参加, 见, 上(课), 听, 看(演出), 讲, 演示, 出差
  The text describes attending/participating, not producing a result.
  Examples: "周五开项目评审会", "明天下午见客户"

TODO — the person must PRODUCE/DELIVER/COMPLETE something.
  Action verbs: 交, 提交, 写完, 做好, 处理, 搞定, 准备, 完成, 整理, 买
  The text describes a deliverable or completion, often with deadline feel.
  Examples: "周五交报告", "下周写完方案", "买生日礼物"

FORCE-TODO signals (regardless of verb):
  Text contains: "之前", "截止", "ddl", "为止", "deadline"
  → type = "todo", put the date in the deadline field.
  Example: "下午五点之前交上报告" → todo

PURE TODO — no date, no time, no deadline:
  → type = "todo", date=null, deadline=null, needs_confirmation=true

DATE FIELD RULE:
  - event: put the date in the "date" field; deadline=null
  - todo: put the deadline date in the "deadline" field (format "YYYY-MM-DD 23:59"); date=null
  - Do NOT put a date in both fields.

When truly unsure, default to "event".

═══════════════════════════════════════

Return ONLY a valid JSON array. No markdown code blocks, no explanations, no other text.

Each item in the array must have this structure:
{{
  "type": "event",
  "title": "具体标题",
  "date": "YYYY-MM-DD",
  "start_time": "14:30",
  "end_time": "16:00",
  "deadline": "YYYY-MM-DD 23:59",
  "location": "具体地点",
  "time_period": "afternoon",
  "priority": "medium",
  "source_text": "该条目的原文片段",
  "confidence": 0.9,
  "needs_confirmation": false
}}

Field descriptions:
- type: "event" or "todo" — see TYPE CLASSIFICATION RULES above
- title: concise summary in Chinese
- date: YYYY-MM-DD — the absolute date for this item, see DATE RESOLUTION below; null if no date can be determined
- start_time: HH:MM (24h), or null
- end_time: HH:MM (24h), or null
- deadline: YYYY-MM-DD HH:MM, or null
- location: place name, or null
- time_period: "morning"(6-11), "noon"(11-13), "afternoon"(13-17), "evening"(17-21), "night"(21-6), or null if exact time given or no time info
- priority: "low", "medium", or "high"
- source_text: copy the exact text fragment from the user message for this item
- confidence: 0.0 to 1.0; 0.9+ when an absolute YYYY-MM-DD date or an exact HH:MM time was already present in the text; at most 0.6 when YOU resolved a date out of a Chinese expression yourself (that is a guess); 0.3 for pure todos
- needs_confirmation: true whenever date AND deadline are both null, or when the date is one YOU resolved from a Chinese expression (a guess). Set it to false ONLY when the text already contained an absolute YYYY-MM-DD date or an exact HH:MM time that you copied.

Critical rules:
- DATE RESOLUTION: relative expressions in the user's text (今天/明天/下周五/3天后…)
  have ALREADY been rewritten to YYYY-MM-DD by the application before you see
  them. Normally copy those absolute dates straight into the date/deadline field.
  Sanity-check them anyway: if an absolute date is already in the past relative
  to TODAY'S CONTEXT and the source text did not mark the past itself
  (上周/上上周/上月底/上月末/上月月底/上上月末/昨天/前天/大前天), it is almost certainly a mis-resolution —
  in that case put null in date/deadline and set needs_confirmation=true instead
  of copying it. A past date that DID come from one of those past-marking words
  is deliberate: copy it and keep needs_confirmation=false.
- Some expressions are deliberately left in Chinese because the correct date
  depends on a holiday calendar or on context the text does not give — for
  example 国庆, 春节, 元旦, 本周末, 下个月中, recurring forms like 每周一 or
  每月3号, and bare day-of-month numbers like 15号 or 3号, which are ambiguous
  (2号线, 3号楼, 30号仓库, 101室 all contain 数字+号/室 and are NOT dates).
- A year-less X月X日 that has ALREADY PASSED is also left in Chinese (e.g. "1月1日"
  said in September). You must NOT roll it forward to next year: that invents a
  year the user never gave, and the text would look exactly as trustworthy as a
  date that was really stated. Such an expression always takes action (b) below.
- For any date expression still written in Chinese that is NOT already
  YYYY-MM-DD, you have exactly two legal actions:
  (a) resolve it yourself using TODAY'S CONTEXT above — and because that is a
      guess, set needs_confirmation=true and confidence to 0.6 or lower; or
  (b) put null in date/deadline and set needs_confirmation=true.
  NEVER invent, guess or approximate a date. A wrong date shown to the user is
  worse than an empty one.
- If one text fragment ends up containing two absolute dates, split it into two
  array items, unless the wording clearly describes a single event.
- Split multi-item input into separate array items
- If the text already contained an absolute YYYY-MM-DD date or an exact HH:MM
  time, copy it into date/deadline, set needs_confirmation to false and
  confidence to 0.9+
- time_period is required when a time expression like "下午"/"晚上" is mentioned but no exact HH:MM is given
- source_text must be the exact text snippet for that specific item only
- Pure todos with no date/time at all MUST be extracted as separate items with needs_confirmation=true
- If the notification contains no events or todos, return []
- Return ONLY the JSON array, nothing else\
"""


def _strip_markdown(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _salvage_json_array(text: str) -> List[dict]:
    """Parse as many COMPLETE objects as possible out of a (possibly truncated) array.

    Walks the text object by object with raw_decode, so a response cut off
    mid-object (e.g. by max_tokens) still yields every item that did finish.
    """
    decoder = json.JSONDecoder()
    start = text.find("[")
    if start == -1:
        return []
    pos = start + 1
    objs: List[dict] = []
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            break
        try:
            obj, pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _extract_json_array(raw: str) -> List:
    """Pull the first complete JSON array out of a model response.

    Tolerates leading/trailing prose, markdown fences anywhere in the text, and
    falls back to salvaging whole objects from a truncated array. Never returns
    [] for a parse failure — that would report "nothing found" instead of
    "parsing failed".

    A candidate array qualifies only if it is empty (a legitimate "no items"
    answer) or holds at least one JSON object. Without that check a stray
    "[1]" citation in prose (e.g. 根据文献[1]…) would be returned as the whole
    schedule and every element would then fail validation.
    """
    text = _strip_markdown(raw)
    if not text:
        raise ValueError("AI 返回了空响应，请重试。")

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", text):
        try:
            data, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(data, list) and (not data or any(isinstance(x, dict) for x in data)):
            return data

    salvaged = _salvage_json_array(text)
    if salvaged:
        _record_parse_warning(
            f"AI 返回的 JSON 数组不完整（可能被 max_tokens 截断），已恢复其中 {len(salvaged)} 条完整数据。"
        )
        return salvaged

    raise ValueError(f"AI 返回的不是有效 JSON 数组：\n{text[:500]}")


def _reset_parse_warnings() -> None:
    """Prepare st.session_state["parse_warnings"] for one parse_notification call.

    CONTRACT (the caller renders st.session_state.get("parse_warnings", [])):
    - key name is exactly "parse_warnings"
    - value is always a List[str]
    - cleared at the START of every parse_notification call, so warnings never
      accumulate across calls
    - cleared in place when possible, so a list reference the caller grabbed
      before the call sees the new entries
    Outside a Streamlit script there is no session_state and this is a no-op.
    """
    try:
        warnings = st.session_state.get("parse_warnings")
        if isinstance(warnings, list):
            del warnings[:]
        else:
            st.session_state["parse_warnings"] = []
    except Exception:  # not running inside a Streamlit script (e.g. tests)
        pass


def _record_parse_warning(message: str) -> None:
    """Queue a per-item problem for the caller (see _reset_parse_warnings).

    The caller re-renders the app (st.rerun), which wipes anything st.warning
    printed during this run, so warnings are parked in session_state instead.
    """
    try:
        # SessionState only implements the mapping dunders it needs — no setdefault
        warnings = st.session_state.get("parse_warnings")
        if not isinstance(warnings, list):
            warnings = []
            st.session_state["parse_warnings"] = warnings
        warnings.append(str(message))
    except Exception:  # not running inside a Streamlit script (e.g. tests)
        st.warning(message)


_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_UNIT_DAYS = {"天": 1, "日": 1, "周": 7, "星期": 7}
_RECURRENCE_MARKERS = "每双隔逢"
_ABS_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_WD = "[" + "".join(_WEEKDAY_MAP) + "]"
_WD_PREFIX = r"(?:周|星期)\s*"
# 每周一 / 隔周 — the lookbehind keeps recurrence out of the weekday rules
_WD_NOT_RECURRING = rf"(?<![{_RECURRENCE_MARKERS}])"
# 上/下/本/这 directly in front means we are looking at a tail of a longer prefix
# (本下周三) rather than a complete expression — refuse to match instead of
# leaving an orphaned prefix behind.
_NO_ORPHAN_PREFIX = r"(?<![上下本这个])"

# An already-absolute date is passed through untouched and, being consumed first,
# is immune to every other rule: a full YYYY-MM-DD is never re-scanned, so
# _resolve_dates is idempotent (2026-10-05号 cannot fuse with a day-of-month).
_ISO_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}\s*[日号]?")
_REL_DAY_RE = re.compile(r"大前天|大后天|明天|明日|后天|前天|今天|今日|昨天")
_MONTH_DAY_RE = re.compile(
    r"(?<!\d)(?:\s*(?P<md_y>\d{4})\s*年)?\s*(?P<md_m>\d{1,2})\s*月\s*(?P<md_d>\d{1,2})\s*[日号]"
)
# 数字+号/日 on its own is NOT resolvable (2号线, 3号楼, 101室, 30号仓库, 第5日), so
# there is deliberately no bare day-of-month rule: the expression is left in
# Chinese for the model, which the prompt tells to resolve-or-null it.
# 月底/月末. Two things the lookbehind has to keep out:
#   - 上/下/本/这 immediately before the match = a prefix the expression owns
#     (see _NO_ORPHAN_PREFIX);
#   - a Chinese numeral immediately before it. Chinese-numeral months are NOT
#     resolvable here, so "三月底"/"十月底" must not be read as THIS month's end.
#   - a 月 immediately before it, i.e. the inner 月 of "五月月末"/"下月 月底": the
#     real start is further left, and matching from here strands the prefix.
#   - a digit immediately before it ("9月月底", "2026年10月底"): a numeric month
#     number is not part of this expression, and matching from here would glue the
#     leftover digit onto the date ("92026-09-30").
# The optional second 月 is what lets 下月月底 be consumed as one expression: the
# prefix (下) followed by TWO 月. Without it the match starts on the inner 月, the
# lookbehind then blocks the real start, and the prefix is left orphaned in front
# of a wrong-month date.
_MONTH_END_RE = re.compile(
    r"(?<![0-9上下本这个零一二两三四五六七八九十月])"
    r"(?P<me_p>上+|下+|本|这个|这)?\s*(?:周|星期)?\s*月\s*月?(?:底|末)"
)
_WD_RANGE_RE = re.compile(
    rf"{_WD_NOT_RECURRING}{_NO_ORPHAN_PREFIX}(?P<wr_p>上+|下+|本|这个|这)?\s*{_WD_PREFIX}(?P<wr_a>{_WD})"
    rf"\s*(?:到|至|~|-|—|–)\s*(?:周|星期)?\s*(?P<wr_b>{_WD})"
)
_WEEKDAY_RE = re.compile(
    rf"{_WD_NOT_RECURRING}{_NO_ORPHAN_PREFIX}(?P<wd_p>上+|下+|本|这个|这)?\s*{_WD_PREFIX}(?P<wd>{_WD})"
)
_LATER_RE = re.compile(
    _WD_NOT_RECURRING
    + r"(?P<n>\d+|[零一二两三四五六七八九十]{1,3})\s*个?\s*(?P<u>天|日|周|星期)\s*[之以]?后"
)


def _cn_int(s: str) -> int:
    """中文数字转 int，覆盖口语常用的一 ~ 九十九。"""
    if not s:
        return 0
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    if "十" not in s:
        return 0
    head, _, tail = s.partition("十")
    return (_CN_DIGITS.get(head, 1) if head else 1) * 10 + _CN_DIGITS.get(tail, 0)


def _resolve_dates(text: str) -> str:
    """Replace relative date expressions in Chinese text with absolute YYYY-MM-DD.

    All rules share ONE combined regex applied in a single left-to-right pass, so
    substituted output is never rescanned and two resolved dates can never be
    merged into one unreadable run (a "、" is inserted if they end up adjacent).
    An already-absolute YYYY-MM-DD is consumed by the first rule and passed
    through verbatim, which makes the whole function idempotent.

    PAST-DATE POLICY (one invariant shared by every rule below):
        An INFERRED date is never in the past. The only exception is text that
        explicitly marks the past — 昨天/前天/大前天 and a 上/上上… prefix
        (上周五, 上上周一, 上月底, 上上月底) — which is resolved into the past on
        purpose. A 本/这 prefix is NOT such an exception: it scopes to this
        Monday-based week, and a day of that week that has already gone rolls
        forward a week (本周一 said on a Tuesday = next Monday), so 本/这 also
        never yields a past date. Therefore a bare 周二 means the next
        strictly-future 周二, never today: a 周二 spoken at 21:00 on Tuesday
        cannot mean today. And a year-less X月X日 that has already passed is NOT
        rolled forward a year — that would be as much a fabrication as leaving
        it in the past, and it would look exactly as trustworthy. Both are left
        in Chinese for the model, which the prompt tells to resolve them or to
        emit null + needs_confirmation. Dates the user spelled out (an ISO
        string, or an explicit 年) are passed through untouched: they were
        stated, not inferred.

    Weekday semantics:
    - 上/本/下/下下 prefix the Monday of that week; bare 周三 (no prefix) means the
      NEXT strictly-future occurrence — from Tuesday, 周三 is tomorrow and 周二
      is next week. It never resolves into the past. 本/这 keep this week's
      Monday as the anchor but roll a day that is already past to next week, so
      they are still never in the past.
    - In a range (周一到周五) the end is derived from the resolved START, so a
      range can never come out reversed. A same-weekday range (周一到周一) is
      zero days wide, not a week.
    - Recurring forms (每周一, 每月3号, 隔周, 每3天后) are left untouched so the
      recurrence survives to the model.
    - Ambiguous expressions (国庆, 春节, 下周末, 下个月, 中文数字月份 such as
      三月底/十月底, bare 3号, 2号线) are never guessed; they are left in Chinese
      for the model, which is told to resolve-or-null them.
    """
    today = date.today()
    today_wd = today.weekday()
    this_monday = today - timedelta(days=today_wd)

    def _d(value: date) -> str:
        return value.strftime("%Y-%m-%d")

    def _try_date(year: int, month: int, day: int):
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def _recurring(m) -> bool:
        # 每 3 个字以内出现 每/双/隔/逢 → 属于复发表达，跳过替换
        return any(c in m.string[max(0, m.start() - 4):m.start()] for c in _RECURRENCE_MARKERS)

    def _week_target(prefix: str, weekday_char: str) -> date:
        wd = _WEEKDAY_MAP[weekday_char]
        if not prefix:
            # 最近一个严格晚于今天的该星期几（今天说的「周二」= 下周二）
            return today + timedelta(days=((wd - today_wd) % 7) or 7)
        if prefix.startswith("上"):
            return this_monday - timedelta(weeks=len(prefix), days=-wd)
        if prefix in ("本", "这", "这个"):
            target = this_monday + timedelta(days=wd)
            # 本周X means "the X of this Monday-based week". Said on a later day
            # that weekday is already gone (本周一 on a Tuesday = yesterday), so
            # roll it to the same weekday next week. This keeps the ONE invariant
            # — an inferred date is never in the past — instead of making 本 an
            # undeclared second exception to it.
            if target < today:
                target += timedelta(weeks=1)
            return target
        return this_monday + timedelta(weeks=len(prefix), days=wd)

    def _rel_day(m):
        offsets = {"今天": 0, "今日": 0, "昨天": -1, "前天": -2, "大前天": -3,
                   "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
        return _d(today + timedelta(days=offsets[m.group(0)]))

    def _pass_through(m):
        return m.group(0)

    def _month_day(m):
        if _recurring(m):
            return None
        year = int(m.group("md_y")) if m.group("md_y") else today.year
        value = _try_date(year, int(m.group("md_m")), int(m.group("md_d")))
        if value is None:
            return None
        # 年份是推断出来的，而这一天已经过去 → 不滚到明年，原样留给模型
        if not m.group("md_y") and value < today:
            return None
        return _d(value)

    def _month_end(m):
        if _recurring(m):
            return None
        prefix = m.group("me_p") or ""
        # 上/上上… explicitly marks the past, so it is resolved INTO the past on
        # purpose (上月底 → last day of last month), exactly like 上周五 → last
        # Friday. 下/下下… moves forward the same number of months.
        months = 0
        if prefix.startswith("上"):
            months = -len(prefix)
        elif prefix.startswith("下"):
            months = len(prefix)
        year, month = today.year, today.month + months
        year += (month - 1) // 12
        month = (month - 1) % 12 + 1
        last_day = (date(year + (month // 12), month % 12 + 1, 1) - timedelta(days=1)).day
        return _d(date(year, month, last_day))

    def _weekday(m):
        if _recurring(m):
            return None
        return _d(_week_target(m.group("wd_p") or "", m.group("wd")))

    def _weekday_range(m):
        if _recurring(m):
            return None
        start = _week_target(m.group("wr_p") or "", m.group("wr_a"))
        # 周一→周一 is zero days wide, not a week
        span = (_WEEKDAY_MAP[m.group("wr_b")] - _WEEKDAY_MAP[m.group("wr_a")]) % 7
        return f"{_d(start)}到{_d(start + timedelta(days=span))}"

    def _n_units_later(m):
        n = int(m.group("n")) if m.group("n").isdigit() else _cn_int(m.group("n"))
        if n <= 0:
            return None
        return _d(today + timedelta(days=n * _UNIT_DAYS[m.group("u")]))

    rules = [
        ("iso", _ISO_RE, _pass_through),
        ("wd_range", _WD_RANGE_RE, _weekday_range),
        ("month_day", _MONTH_DAY_RE, _month_day),
        ("rel_day", _REL_DAY_RE, _rel_day),
        ("month_end", _MONTH_END_RE, _month_end),
        ("weekday", _WEEKDAY_RE, _weekday),
        ("later", _LATER_RE, _n_units_later),
    ]
    combined = re.compile("|".join(f"(?P<{name}>{pattern.pattern})" for name, pattern, _ in rules))

    state = {"end": -1, "text": ""}

    def _dispatch(m):
        rep = m.group(0)
        for name, _, fn in rules:
            if m.group(name) is not None:
                rep = fn(m) or m.group(0)
                break
        # 相邻的两个已解析日期之间保留分隔符，模型才能分辨这是两天
        if m.start() == state["end"] and _ABS_DATE.search(state["text"]) and _ABS_DATE.search(rep):
            rep = "、" + rep
        state["end"], state["text"] = m.end(), rep
        return rep

    return combined.sub(_dispatch, text)


_MISSING_KEY_HINT = (
    "未配置 DEEPSEEK_API_KEY。\n"
    "· 本地运行：编辑项目根目录下的 .streamlit/secrets.toml，写入 DEEPSEEK_API_KEY = \"sk-...\"\n"
    "· Streamlit Cloud：App → Settings → Secrets，添加 DEEPSEEK_API_KEY 键后重新部署"
)


def get_client() -> OpenAI:
    try:
        # Secrets.get raises StreamlitSecretNotFoundError (not a KeyError) when the
        # key — or the whole secrets file — is missing, so it has to be caught here.
        api_key = st.secrets.get("DEEPSEEK_API_KEY")
    except StreamlitSecretNotFoundError:
        raise ValueError(_MISSING_KEY_HINT) from None
    if not api_key:
        raise ValueError(_MISSING_KEY_HINT)
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def parse_notification(text: str) -> List[ScheduleItem]:
    # Reset first, so this call's warnings are the only ones left standing even
    # if get_client() or the API call below raises.
    _reset_parse_warnings()

    client = get_client()

    text = _resolve_dates(text)

    now = datetime.now()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        today_date=now.strftime("%Y-%m-%d"),
        today_weekday=weekday_cn[now.weekday()],
        current_time=now.strftime("%H:%M"),
    )

    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
        max_tokens=4096,
    )

    raw = response.choices[0].message.content or ""
    data = _extract_json_array(raw)

    if not isinstance(data, list):
        raise ValueError(f"AI 返回的不是数组:\n{raw[:500]}")

    items: List[ScheduleItem] = []
    for idx, obj in enumerate(data):
        if not isinstance(obj, dict):
            _record_parse_warning(f"第 {idx + 1} 条不是 JSON 对象，已跳过: {type(obj).__name__}")
            continue
        try:
            obj.pop("id", None)
            obj.pop("is_completed", None)
            obj.pop("deleted", None)
            obj.pop("created_at", None)
            obj.pop("updated_at", None)
            item = ScheduleItem(**obj)
            items.append(item)
        except Exception as e:
            _record_parse_warning(f"第 {idx + 1} 条数据校验失败，已跳过: {e}")

    return items
