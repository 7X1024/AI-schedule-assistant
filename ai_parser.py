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

Date resolution rules (use today's context to resolve into YYYY-MM-DD):
- "今天" → {today_date}
- "明天" → tomorrow's date
- "后天" → the day after tomorrow
- "本周六"/"本周日" → the specific date this week
- "下周三" → the specific date next week
- "周五" (no qualifier) → the upcoming Friday of this week

Only set needs_confirmation=true and date=null for TRULY ambiguous expressions:
- "下周" (which week?)
- "尽快" / "近期" / "有空的时候"
- "周五之前" (which Friday?)
- Tasks with NO date/time hint at all (e.g. "记得买礼物")
- vague time like "下午" without any date reference

═══════════════════════════════════════
CRITICAL: SPLITTING RULES
═══════════════════════════════════════
When the user provides MULTIPLE events/todos in ONE message, split them
into separate JSON objects. Natural boundaries include:
  - Line breaks (each line is usually one item)
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

Example input:
"明天下午3点在会议室A开项目评审会，周五之前提交周报，下周一找张总讨论预算，记得买生日礼物"

Example output:
[
  {{
    "type": "event",
    "title": "项目评审会",
    "date": "2026-06-02",
    "start_time": "15:00",
    "end_time": null,
    "deadline": null,
    "location": "会议室A",
    "time_period": null,
    "priority": "medium",
    "source_text": "明天下午3点在会议室A开项目评审会",
    "confidence": 0.95,
    "needs_confirmation": false
  }},
  {{
    "type": "todo",
    "title": "提交周报",
    "date": null,
    "start_time": null,
    "end_time": null,
    "deadline": "2026-06-05 23:59",
    "location": null,
    "time_period": null,
    "priority": "medium",
    "source_text": "周五之前提交周报",
    "confidence": 0.9,
    "needs_confirmation": false
  }},
  {{
    "type": "event",
    "title": "与张总讨论预算",
    "date": "2026-06-08",
    "start_time": null,
    "end_time": null,
    "deadline": null,
    "location": null,
    "time_period": null,
    "priority": "medium",
    "source_text": "下周一找张总讨论预算",
    "confidence": 0.9,
    "needs_confirmation": false
  }},
  {{
    "type": "todo",
    "title": "买生日礼物",
    "date": null,
    "start_time": null,
    "end_time": null,
    "deadline": null,
    "location": null,
    "time_period": null,
    "priority": "low",
    "source_text": "记得买生日礼物",
    "confidence": 0.7,
    "needs_confirmation": true
  }}
]
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
- type: "event" for scheduled items (has date/time), "todo" for tasks without a fixed time slot (has deadline or no time at all)
- title: concise summary in Chinese
- date: YYYY-MM-DD, resolved from relative dates using today's context; null if unresolvable
- start_time: HH:MM (24h), or null
- end_time: HH:MM (24h), or null
- deadline: YYYY-MM-DD HH:MM, or null (for todos with a due date)
- location: place name, or null
- time_period: "morning"(6-11), "noon"(11-13), "afternoon"(13-17), "evening"(17-21), "night"(21-6), or null if exact time given or no time info
- priority: "low", "medium", or "high". Use context clues: "紧急"/"立即"/"马上" → high; normal tasks → medium; "有空"/"抽空"/"顺便" → low
- source_text: the EXACT text fragment from the input for THIS item only, not the whole message
- confidence: 0.0 to 1.0. Use 0.9+ when date/time is clear; 0.6-0.8 when moderately ambiguous; 0.3-0.5 for pure todos with no time hint
- needs_confirmation: true ONLY if date/time is truly unresolvable even with today's context, or for pure todos with no time info at all

Critical rules:
- Split multi-item input into separate array items (see SPLITTING RULES above)
- Use today's context to resolve relative dates into exact YYYY-MM-DD whenever possible
- If a date IS resolvable, set needs_confirmation to false and confidence to 0.9+
- time_period is required when a time like "下午"/"晚上" is mentioned but no exact HH:MM is given
- source_text must be the exact text snippet for that specific item only
- Pure todos with no date/time at all MUST still be extracted as separate items with needs_confirmation=true
- If the notification contains no events or todos at all, return []
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
