from __future__ import annotations

import json
import re
from datetime import datetime
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

You MUST use the current date context to resolve relative dates into YYYY-MM-DD format:
- "今天" → {today_date}
- "明天" → tomorrow's date
- "本周六" / "本周日" → the specific date this week
- "下周三" → the specific date next week
- "后天" → the day after tomorrow

Only set needs_confirmation=true and date=null for TRULY ambiguous expressions like:
- "下周" (which week?)
- "尽快" / "近期" / "有空的时候"
- "周五之前" (which Friday?)

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
  "source_text": "原始通知中的相关文本片段",
  "confidence": 0.9,
  "needs_confirmation": false
}}

Field descriptions:
- type: "event" for scheduled events with a time, "todo" for tasks/deadlines without a fixed time slot
- title: concise summary
- date: YYYY-MM-DD, resolve from relative dates using today's context
- start_time: HH:MM (24h), or null
- end_time: HH:MM (24h), or null
- deadline: YYYY-MM-DD HH:MM, or null
- location: place name, or null
- time_period: "morning"(6-11), "noon"(11-13), "afternoon"(13-17), "evening"(17-21), "night"(21-6), or null if exact time is given
- priority: "low", "medium", or "high"
- source_text: the EXACT text snippet from the notification this item was extracted from
- confidence: 0.0 to 1.0
- needs_confirmation: true only if date/time is truly unresolvable even with today's context

Critical rules:
- Use today's context to resolve relative dates into exact YYYY-MM-DD whenever possible
- If a date IS resolvable, set needs_confirmation to false and confidence to 0.9+
- time_period is required when a time like "下午" / "晚上" is mentioned but no exact HH:MM is given
- source_text must be the exact text snippet from the notification
- If the notification contains no events or todos, return []
- Return ONLY the JSON array, nothing else\
"""


def _strip_markdown(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def get_client() -> OpenAI:
    api_key = st.secrets.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("请在 .streamlit/secrets.toml 中设置 DEEPSEEK_API_KEY")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def parse_notification(text: str) -> List[ScheduleItem]:
    client = get_client()

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
