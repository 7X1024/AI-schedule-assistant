from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import List

import streamlit as st
from openai import OpenAI

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

Return ONLY a valid JSON array. No markdown code blocks, no explanations, no other text.

Each item in the array must have this structure:
{{
  "type": "event",
  "title": "具体标题",
  "date": "2026-05-28",
  "start_time": "14:30",
  "end_time": "16:00",
  "deadline": "2026-06-01 23:59",
  "location": "具体地点",
  "time_period": "afternoon",
  "priority": "medium",
  "source_text": "该条目的原文片段",
  "confidence": 0.9,
  "needs_confirmation": false
}}

Field descriptions:
- type: "event" for scheduled items (has date/time), "todo" for tasks without a fixed time slot (has deadline or no time info at all)
- title: concise summary in Chinese
- date: YYYY-MM-DD — copy the date that already appears in the text; null only if no date is present
- start_time: HH:MM (24h), or null
- end_time: HH:MM (24h), or null
- deadline: YYYY-MM-DD HH:MM, or null
- location: place name, or null
- time_period: "morning"(6-11), "noon"(11-13), "afternoon"(13-17), "evening"(17-21), "night"(21-6), or null if exact time given or no time info
- priority: "low", "medium", or "high"
- source_text: copy the exact text fragment from the user message for this item
- confidence: 0.0 to 1.0; 0.9+ when date is present in text; 0.5 when only time_period is given; 0.3 for pure todos
- needs_confirmation: true ONLY if date is null AND no time_period, or for pure todos with no date/time at all

Critical rules:
- Dates in the user's text are ALREADY in YYYY-MM-DD format. Copy them directly into the date field. Do NOT compute or recalculate dates.
- Split multi-item input into separate array items
- If a date is present, set needs_confirmation to false and confidence to 0.9+
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


def _resolve_dates(text: str) -> str:
    """Replace relative date expressions in Chinese text with absolute YYYY-MM-DD."""
    today = date.today()
    weekday = today.weekday()
    this_monday = today - timedelta(days=weekday)

    SHORT_DAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    SHORT_RE = "|".join(SHORT_DAY)

    def _d(d: date) -> str:
        return d.strftime("%Y-%m-%d")

    replacements: dict[str, str] = {
        "今天": _d(today),
        "明日": _d(today + timedelta(days=1)),
        "明天": _d(today + timedelta(days=1)),
        "后天": _d(today + timedelta(days=2)),
        "大后天": _d(today + timedelta(days=3)),
    }

    for m in re.finditer(r"(\d+)\s*天[之以]?后", text):
        n = int(m.group(1))
        replacements[m.group(0)] = _d(today + timedelta(days=n))

    for m in re.finditer(r"(\d+)\s*[周個]\s*[之以]?后", text):
        n = int(m.group(1))
        replacements[m.group(0)] = _d(today + timedelta(weeks=n))

    for m in re.finditer(rf"(下*)(?:本)?\s*周\s*({SHORT_RE})", text):
        weeks_ahead = len(m.group(1))
        target = this_monday + timedelta(weeks=weeks_ahead, days=SHORT_DAY[m.group(2)])
        replacements[m.group(0)] = _d(target)

    for m in re.finditer(rf"星期\s*({SHORT_RE})", text):
        target = this_monday + timedelta(days=SHORT_DAY[m.group(1)])
        replacements[m.group(0)] = _d(target)

    for pattern, replacement in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(pattern, replacement)

    return text


def get_client() -> OpenAI:
    api_key = st.secrets.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("请在 .streamlit/secrets.toml 中设置 DEEPSEEK_API_KEY")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def parse_notification(text: str) -> List[ScheduleItem]:
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
    raw = _strip_markdown(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if not raw:
            return []
        raise ValueError(f"AI 返回的不是有效 JSON:\n{raw[:500]}")

    if not isinstance(data, list):
        raise ValueError(f"AI 返回的不是数组:\n{raw[:500]}")

    items: List[ScheduleItem] = []
    for idx, obj in enumerate(data):
        try:
            obj.pop("id", None)
            obj.pop("is_completed", None)
            obj.pop("deleted", None)
            obj.pop("created_at", None)
            obj.pop("updated_at", None)
            item = ScheduleItem(**obj)
            items.append(item)
        except Exception as e:
            st.warning(f"第 {idx + 1} 条数据校验失败，已跳过: {e}")

    return items
