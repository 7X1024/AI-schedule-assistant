from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

import streamlit as st

from ai_parser import parse_notification
from models import ScheduleItem
from sheets_storage import delete_event, delete_todo, load_events, load_todos, save_event, save_todo, toggle_todo

st.set_page_config(
    page_title="AI 日程助手",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    html, body, [class*="st-"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    #MainMenu, footer, header { visibility: hidden; }

    .stApp { background: #f5f5f7; }

    /* ── responsive columns ── */
    @media (max-width: 768px) {
        [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
        }
        [data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            flex: 1 1 100% !important;
            max-width: 100% !important;
        }
    }

    /* ── week columns: stack on mobile ── */
    @media (max-width: 768px) {
        .week-grid > [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
        }
        .week-grid > [data-testid="stHorizontalBlock"] > [data-testid="column"] {
            flex: 1 1 48% !important;
            max-width: 48% !important;
        }
    }

    /* ── cards ── */
    .card {
        background: #ffffff;
        border-radius: 12px;
        padding: 14px 16px;
        margin-bottom: 10px;
        border: 1px solid #e8e8ed;
        transition: box-shadow 0.15s;
    }
    .card:hover { box-shadow: 0 2px 12px rgba(0,0,0,0.06); }

    .card-title {
        font-size: 15px;
        font-weight: 600;
        color: #1a1a1a;
        margin-bottom: 6px;
    }
    .card-meta {
        font-size: 12.5px;
        color: #888;
        line-height: 1.5;
    }
    .card-source {
        font-size: 11.5px;
        color: #aaa;
        margin-top: 6px;
        font-style: italic;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    /* ── priority badges ── */
    .badge {
        display: inline-block;
        font-size: 11px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 10px;
        margin-left: 6px;
    }
    .badge-high   { background: #fef2f2; color: #dc2626; }
    .badge-medium { background: #fffbeb; color: #d97706; }
    .badge-low    { background: #f3f4f6; color: #6b7280; }

    /* ── section headers ── */
    .section-title {
        font-size: 17px;
        font-weight: 700;
        color: #1a1a1a;
        margin-bottom: 14px;
    }

    /* ── preview panel ── */
    .preview-card {
        background: #f0f4ff;
        border: 1px dashed #6366f1;
        border-radius: 12px;
        padding: 14px 16px;
        margin-bottom: 8px;
    }

    /* ── empty state ── */
    .empty-state {
        text-align: center;
        padding: 32px 16px;
        color: #bbb;
        font-size: 14px;
    }

    /* ── textarea tweaks ── */
    textarea {
        border-radius: 12px !important;
        border: 1px solid #e0e0e5 !important;
        background: #ffffff !important;
        font-size: 14px !important;
    }
    textarea:focus {
        border-color: #6366f1 !important;
        box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.15) !important;
    }

    /* ── buttons ── */
    .stButton > button {
        border-radius: 10px !important;
        font-weight: 600 !important;
        font-size: 14px !important;
        transition: all 0.15s !important;
    }

    /* ── confirm badge ── */
    .confirm-badge {
        display: inline-block;
        background: #fef2f2;
        color: #dc2626;
        font-size: 11px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 4px;
    }

    /* ── day column header ── */
    .day-header {
        text-align: center;
        font-size: 12px;
        font-weight: 700;
        color: #6366f1;
        padding: 8px 0;
        margin-bottom: 6px;
        border-bottom: 2px solid #e8e8ed;
    }
    .day-header-today {
        color: #dc2626;
        border-bottom-color: #dc2626;
    }
    .day-header-date {
        font-size: 10px;
        color: #999;
        font-weight: 500;
    }

    /* ── compact date input ── */
    .stDateInput > div { width: 180px !important; }
</style>
""",
    unsafe_allow_html=True,
)

# ── session state init ───────────────────────────────────────────────────────
if "events" not in st.session_state:
    st.session_state.events = load_events()
if "todos" not in st.session_state:
    st.session_state.todos = load_todos()
if "preview_items" not in st.session_state:
    st.session_state.preview_items: List[ScheduleItem] = []


def refresh_data() -> None:
    st.session_state.events = load_events()
    st.session_state.todos = load_todos()


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse_date(d: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(d) if d else None
    except (ValueError, TypeError):
        return None


def _card_html(item: ScheduleItem, extra: str = "") -> str:
    priority_label = {"high": "高", "medium": "中", "low": "低"}.get(item.priority, "")
    source = item.source_text.replace("\n", " ")[:80]

    if item.type == "event":
        time_parts = []
        if item.start_time:
            t = item.start_time
            if item.end_time:
                t += f" - {item.end_time}"
            time_parts.append(t)
        elif item.time_period:
            period_labels = {"morning": "上午", "noon": "中午", "afternoon": "下午", "evening": "晚上", "night": "半夜"}
            time_parts.append(period_labels.get(item.time_period, item.time_period))
        if item.location:
            time_parts.append(f"📍 {item.location}")
        meta = " · ".join(time_parts) if time_parts else ""
    else:
        meta = ""
        if item.deadline:
            meta = f"截止: {item.deadline}"
        if item.location:
            meta += f" · 📍 {item.location}" if meta else f"📍 {item.location}"

    confirm = ""
    if item.needs_confirmation:
        confirm = '<span class="confirm-badge">需确认</span>'

    return f"""
    <div class="card {extra}">
        <div class="card-title">{item.title} {confirm} <span class="badge badge-{item.priority}">{priority_label}</span></div>
        <div class="card-meta">{meta}</div>
        <div class="card-source">原文: {source}</div>
    </div>
    """


def _compact_card_html(item: ScheduleItem) -> str:
    priority_color = {"high": "#ef4444", "medium": "#f59e0b", "low": "#9ca3af"}
    color = priority_color.get(item.priority, "#9ca3af")

    time_str = ""
    if item.start_time:
        time_str = item.start_time
        if item.end_time:
            time_str += f"-{item.end_time}"
    elif item.time_period:
        period_labels = {"morning": "上午", "noon": "中午", "afternoon": "下午", "evening": "晚上", "night": "半夜"}
        time_str = period_labels.get(item.time_period, "")

    meta_parts = [time_str] if time_str else []
    if item.location:
        meta_parts.append(item.location)
    meta = " · ".join(meta_parts)

    confirm = '<span style="background:#fef2f2;color:#dc2626;font-size:9px;padding:0 3px;border-radius:2px;">!</span>' if item.needs_confirmation else ""

    return f"""
    <div style="background:#fff;border-radius:6px;padding:6px 8px;margin-bottom:5px;border-left:2px solid {color};font-size:11px;line-height:1.4;">
        <div style="font-weight:600;color:#1a1a1a;">{item.title} {confirm}</div>
        <div style="color:#888;font-size:10px;">{meta}</div>
    </div>
    """


def render_item_card(
    item: ScheduleItem,
    compact: bool = False,
    extra: str = "",
    faded: bool = False,
    show_source: bool = True,
) -> None:
    if compact:
        html = _compact_card_html(item)
    else:
        html = _card_html(item, extra)

    if faded:
        html = html.replace('class="card', 'class="card" style="opacity:0.55;"')

    st.markdown(html, unsafe_allow_html=True)

    if show_source and item.source_text and item.source_text.strip():
        with st.popover("", icon="📎", use_container_width=False):
            st.text(item.source_text)


# ── header ───────────────────────────────────────────────────────────────────
st.markdown(
    '<h1 style="font-size:28px;font-weight:700;color:#1a1a1a;margin-bottom:0;">📅 AI 日程助手</h1>'
    '<p style="color:#999;font-size:14px;margin-bottom:4px;">粘贴通知文本，AI 自动提取日程与待办</p>',
    unsafe_allow_html=True,
)

event_count = len(st.session_state.events)
all_todo_count = len(st.session_state.todos)
active_todo_count = sum(1 for t in st.session_state.todos if not t.is_completed)
completed_todo_count = sum(1 for t in st.session_state.todos if t.is_completed)
if event_count > 0 or all_todo_count > 0:
    st.markdown(
        f'<p style="color:#bbb;font-size:12px;margin-bottom:16px;">{event_count} 条日程 · {active_todo_count} 条待办 · {completed_todo_count} 条已完成</p>',
        unsafe_allow_html=True,
    )
else:
    st.markdown('<p style="color:#bbb;font-size:12px;margin-bottom:16px;">暂无数据</p>', unsafe_allow_html=True)

# ── 3-column layout ──────────────────────────────────────────────────────────
col_input, col_events, col_todos = st.columns([1, 1.2, 0.8])

# ══════════════════════════════════════════════════════════════════════════════
# LEFT: Input + Preview
# ══════════════════════════════════════════════════════════════════════════════
with col_input:
    st.markdown('<div class="section-title">📋 通知输入</div>', unsafe_allow_html=True)

    notification_text = st.text_area(
        "通知文本",
        height=240,
        placeholder="在此粘贴通知、公告、邮件、群聊消息…",
        label_visibility="collapsed",
        key="ta_input",
    )

    btn1, btn2 = st.columns(2)
    with btn1:
        if st.button("🔍 识别通知", use_container_width=True, type="primary"):
            if not notification_text.strip():
                st.warning("请先粘贴通知文本")
            else:
                with st.spinner("AI 正在识别…"):
                    try:
                        result = parse_notification(notification_text)
                        st.session_state.preview_items = result
                        if not result:
                            st.info("未识别到日程或待办事项")
                    except Exception as e:
                        st.error(f"识别失败: {e}")
                st.rerun()

    with btn2:
        if st.button("清空输入", use_container_width=True):
            if "ta_input" in st.session_state:
                del st.session_state["ta_input"]
            st.session_state.preview_items = []
            st.rerun()

    # ── Preview area ──
    if st.session_state.preview_items:
        st.divider()
        st.markdown('<div class="section-title">👀 识别预览</div>', unsafe_allow_html=True)
        st.caption("请确认以下内容，确认后才会保存")

        for i, item in enumerate(st.session_state.preview_items):
            render_item_card(item, extra="preview-card")

        col_confirm, col_cancel = st.columns(2)
        with col_confirm:
            if st.button("✅ 确认保存", use_container_width=True, type="primary"):
                saved = 0
                for item in st.session_state.preview_items:
                    if item.type == "event":
                        save_event(item)
                    elif item.type == "todo":
                        save_todo(item)
                    saved += 1
                st.session_state.preview_items = []
                refresh_data()
                st.toast(f"已保存 {saved} 条记录", icon="✅")
                st.rerun()
        with col_cancel:
            if st.button("取消", use_container_width=True):
                st.session_state.preview_items = []
                st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# MIDDLE: Schedule
# ══════════════════════════════════════════════════════════════════════════════
with col_events:
    today = date.today()
    all_events = st.session_state.events

    today_events = [
        e for e in all_events if _parse_date(e.date) == today
    ]
    today_events.sort(key=lambda e: (e.start_time or "99:99", e.title))

    # ── Today ──
    st.markdown('<div class="section-title">📍 今日日程</div>', unsafe_allow_html=True)
    st.caption(today.strftime("%Y年%m月%d日"))

    if today_events:
        for event in today_events:
            render_item_card(event)

            col_del1, _ = st.columns([1, 9])
            with col_del1:
                if st.button("🗑", key=f"del_today_{event.id}", help="删除"):
                    delete_event(event.id)
                    refresh_data()
                    st.rerun()
    else:
        st.markdown(
            '<div class="empty-state">今天暂无日程 🎉</div>', unsafe_allow_html=True
        )

    # ── Week Schedule (horizontal calendar) ──
    st.divider()
    st.markdown('<div class="section-title">📆 周日程</div>', unsafe_allow_html=True)

    selected_date = st.date_input(
        "选择一周",
        value=today,
        label_visibility="collapsed",
        key="week_selector",
    )

    sel_monday = selected_date - timedelta(days=selected_date.weekday())
    sel_sunday = sel_monday + timedelta(days=6)
    st.caption(f"{sel_monday.strftime('%m/%d')} - {sel_sunday.strftime('%m/%d')}")

    week_events = []
    for e in all_events:
        d = _parse_date(e.date)
        if d is not None and sel_monday <= d <= sel_sunday:
            week_events.append(e)
    week_events.sort(key=lambda e: (e.start_time or "99:99", e.title))

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    if week_events:
        day_cols = st.columns(7)
        for i in range(7):
            day = sel_monday + timedelta(days=i)
            is_today = day == today
            day_events = [e for e in week_events if e.date == day.isoformat()]

            with day_cols[i]:
                header_class = "day-header day-header-today" if is_today else "day-header"
                st.markdown(
                    f'<div class="{header_class}">'
                    f'{weekday_names[i]}'
                    f'<div class="day-header-date">{day.strftime("%m/%d")}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                if day_events:
                    for event in day_events:
                        render_item_card(event, compact=True, show_source=False)
                        col_del_w, _ = st.columns([1, 5])
                        with col_del_w:
                            if st.button("✕", key=f"del_w_{event.id}", help="删除"):
                                delete_event(event.id)
                                refresh_data()
                                st.rerun()
                else:
                    st.markdown(
                        '<div style="text-align:center;color:#ddd;font-size:11px;padding:8px;">—</div>',
                        unsafe_allow_html=True,
                    )
    else:
        st.markdown(
            '<div class="empty-state">该周暂无其他日程</div>', unsafe_allow_html=True
        )

    # ── Unscheduled ──
    today_ids = {e.id for e in today_events}
    in_week_ids = {e.id for e in week_events}
    unscheduled_events = [
        e for e in all_events if e.id not in today_ids and e.id not in in_week_ids
    ]
    unscheduled_events.sort(key=lambda e: (e.priority != "high", e.priority != "medium", e.title))

    if unscheduled_events:
        st.divider()
        st.markdown(
            '<div class="section-title" style="color:#f59e0b;">⚠️ 待确认 / 未安排</div>',
            unsafe_allow_html=True,
        )
        st.caption("这些日程日期不明确，需要手动确认")

        for event in unscheduled_events:
            render_item_card(event)

            col_del3, _ = st.columns([1, 9])
            with col_del3:
                if st.button("🗑", key=f"del_unsched_{event.id}", help="删除"):
                    delete_event(event.id)
                    refresh_data()
                    st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# RIGHT: Todos
# ══════════════════════════════════════════════════════════════════════════════
with col_todos:
    st.markdown('<div class="section-title">📝 待办事项</div>', unsafe_allow_html=True)

    all_todos = st.session_state.todos
    active_todos = [t for t in all_todos if not t.is_completed]
    completed_todos = [t for t in all_todos if t.is_completed]

    active_todos.sort(
        key=lambda t: (
            t.priority != "high",
            t.priority != "medium",
            t.deadline or "9999-99-99",
            t.title,
        )
    )

    if active_todos:
        for todo in active_todos:
            render_item_card(todo)

            col_done, col_del3t, _ = st.columns([1, 1, 8])
            with col_done:
                if st.button("✅", key=f"done_{todo.id}", help="标记完成"):
                    toggle_todo(todo.id)
                    refresh_data()
                    st.rerun()
            with col_del3t:
                if st.button("🗑", key=f"del_todo_{todo.id}", help="删除"):
                    delete_todo(todo.id)
                    refresh_data()
                    st.rerun()
    else:
        st.markdown(
            '<div class="empty-state">暂无待办事项 ✨</div>', unsafe_allow_html=True
        )

    # ── Completed ──
    if completed_todos:
        st.divider()
        st.markdown(
            '<div class="section-title" style="color:#999;">✅ 已完成</div>',
            unsafe_allow_html=True,
        )
        for todo in completed_todos:
            render_item_card(todo, faded=True)
            col_undo, col_del4, _ = st.columns([1, 1, 8])
            with col_undo:
                if st.button("↩", key=f"undo_{todo.id}", help="恢复未完成"):
                    toggle_todo(todo.id)
                    refresh_data()
                    st.rerun()
            with col_del4:
                if st.button("🗑", key=f"del_done_{todo.id}", help="删除"):
                    delete_todo(todo.id)
                    refresh_data()
                    st.rerun()
