from __future__ import annotations

import html
from datetime import date, timedelta
from typing import List, Optional

import streamlit as st

from ai_parser import parse_notification
from models import ScheduleItem
from sheets_storage import delete_event, delete_todo, load_events, load_todos, save_event, save_todo, set_current_user, toggle_todo, update_event

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

    /* ── native clickable source details ── */
    .source-details {
        margin: 0 0 10px 0;
    }
    .source-details > summary {
        list-style: none;
        cursor: pointer;
    }
    .source-details > summary::-webkit-details-marker {
        display: none;
    }
    .source-details summary::marker {
        content: "";
    }
    .source-details .card,
    .source-details .compact-card {
        margin-bottom: 0;
    }
    .source-full {
        margin-top: 6px;
        padding: 10px 12px;
        border-radius: 10px;
        background: #ffffff;
        border: 1px solid #e8e8ed;
        color: #555;
        font-size: 12px;
        line-height: 1.55;
        white-space: pre-line;
        word-break: break-word;
        overflow-wrap: break-word;
    }
</style>
""",
    unsafe_allow_html=True,
)

if not st.session_state.get("pc_mode", False):
    st.markdown(
        """
    <style>
        [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
        }
        [data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            flex: 1 1 100% !important;
            max-width: 100% !important;
        }
        .week-grid > [data-testid="stHorizontalBlock"] > [data-testid="column"] {
            flex: 1 1 48% !important;
            max-width: 48% !important;
        }
    </style>
    """,
        unsafe_allow_html=True,
    )

# ── auth ──────────────────────────────────────────────────────────────────────
ACCOUNTS = {"7X": "123456", "Jasper": "888888888"}

if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.markdown(
        '<h1 style="font-size:28px;font-weight:700;color:#1a1a1a;margin-bottom:24px;">📅 AI 日程助手</h1>',
        unsafe_allow_html=True,
    )
    col_lg, _ = st.columns([1, 2])
    with col_lg:
        username = st.text_input("用户名", key="login_user")
        password = st.text_input("密码", type="password", key="login_pass")
        if st.button("登录", use_container_width=True, type="primary"):
            if username in ACCOUNTS and ACCOUNTS[username] == password:
                st.session_state.user = username
                st.rerun()
            else:
                st.error("用户名或密码错误")
    st.stop()

set_current_user(st.session_state.user)

# ── session state init ───────────────────────────────────────────────────────
if "pc_mode" not in st.session_state:
    st.session_state.pc_mode = False
if "events" not in st.session_state:
    st.session_state.events = load_events()
if "todos" not in st.session_state:
    st.session_state.todos = load_todos()
if "preview_items" not in st.session_state:
    st.session_state.preview_items: List[ScheduleItem] = []
if "input_text_val" not in st.session_state:
    st.session_state.input_text_val = ""
if "selected_source_item" not in st.session_state:
    st.session_state.selected_source_item: Optional[ScheduleItem] = None


def refresh_data() -> None:
    st.session_state.events = load_events()
    st.session_state.todos = load_todos()


def clear_input() -> None:
    st.session_state.input_text_val = ""
    st.session_state["ta_input"] = ""
    st.session_state.preview_items = []


def _on_input_change() -> None:
    st.session_state.input_text_val = st.session_state["ta_input"]


def select_source(item: ScheduleItem) -> None:
    st.session_state.selected_source_item = item


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse_date(d: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(d) if d else None
    except (ValueError, TypeError):
        return None


def _card_html(item: ScheduleItem, extra: str = "") -> str:
    priority_label = {"high": "高", "medium": "中", "low": "低"}.get(item.priority, "")
    safe_title = html.escape(item.title)
    source = html.escape(item.source_text.replace("\n", " "))[:80]

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
            time_parts.append(f"📍 {html.escape(item.location)}")
        meta = " · ".join(time_parts) if time_parts else ""
    else:
        meta = ""
        if item.deadline:
            meta = f"截止: {html.escape(item.deadline)}"
        if item.location:
            meta += f" · 📍 {html.escape(item.location)}" if meta else f"📍 {html.escape(item.location)}"

    confirm = ""
    if item.needs_confirmation:
        confirm = '<span class="confirm-badge">需确认</span>'

    return f"""
    <div class="card {extra}">
        <div class="card-title">{safe_title} {confirm} <span class="badge badge-{item.priority}">{priority_label}</span></div>
        <div class="card-meta">{meta}</div>
        <div class="card-source">原文: {source}</div>
    </div>
    """


def _compact_card_html(item: ScheduleItem) -> str:
    priority_color = {"high": "#ef4444", "medium": "#f59e0b", "low": "#9ca3af"}
    color = priority_color.get(item.priority, "#9ca3af")
    safe_title = html.escape(item.title)

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
        meta_parts.append(html.escape(item.location))
    meta = " · ".join(meta_parts)

    confirm = '<span style="background:#fef2f2;color:#dc2626;font-size:9px;padding:0 3px;border-radius:2px;">!</span>' if item.needs_confirmation else ""

    return f"""
    <div class="compact-card" style="background:#fff;border-radius:6px;padding:6px 8px;margin-bottom:5px;border-left:2px solid {color};font-size:11px;line-height:1.4;">
        <div style="font-weight:600;color:#1a1a1a;">{safe_title} {confirm}</div>
        <div style="color:#888;font-size:10px;">{meta}</div>
    </div>
    """


def _source_details_html(card_html: str, source_text: str) -> str:
    safe_source = html.escape(source_text)
    return f"""
    <details class="source-details">
        <summary>{card_html}</summary>
        <div class="source-full">{safe_source}</div>
    </details>
    """


def render_item_card(
    item: ScheduleItem,
    compact: bool = False,
    extra: str = "",
    faded: bool = False,
    show_source: bool = True,
) -> None:
    has_source = bool(show_source and item.source_text and item.source_text.strip())

    if compact:
        card_html = _compact_card_html(item)
    else:
        card_html = _card_html(item, extra)

    if faded:
        card_html = card_html.replace('class="card', 'class="card" style="opacity:0.55;"')

    if has_source:
        st.markdown(_source_details_html(card_html, item.source_text), unsafe_allow_html=True)
    else:
        st.markdown(card_html, unsafe_allow_html=True)


# ── header ───────────────────────────────────────────────────────────────────
col_head, col_user_info, col_mode_btn = st.columns([7, 1.5, 1.5])
with col_head:
    st.markdown(
        '<h1 style="font-size:28px;font-weight:700;color:#1a1a1a;margin-bottom:0;">📅 AI 日程助手</h1>'
        '<p style="color:#999;font-size:14px;margin-bottom:4px;">粘贴通知文本，AI 自动提取日程与待办</p>',
        unsafe_allow_html=True,
    )
with col_user_info:
    st.markdown(
        f'<div style="text-align:right;padding-top:6px;">'
        f'<span style="color:#6366f1;font-weight:600;">👤 {st.session_state.user}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if st.button("退出", key="logout_btn"):
        st.session_state.user = None
        st.session_state.events = []
        st.session_state.todos = []
        st.rerun()
with col_mode_btn:
    mode_label = "🖥️" if st.session_state.pc_mode else "📱"
    mode_help = "切换为PC版" if not st.session_state.pc_mode else "切换为手机版"
    if st.button(mode_label, key="toggle_pc_mode", help=mode_help):
        st.session_state.pc_mode = not st.session_state.pc_mode
        st.rerun()

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

today = date.today()

# ── 3-column layout ──────────────────────────────────────────────────────────
col_input, col_events, col_todos = st.columns([1, 1.2, 0.8])

# ══════════════════════════════════════════════════════════════════════════════
# LEFT: Input + Preview
# ══════════════════════════════════════════════════════════════════════════════
with col_input:
    st.markdown('<div class="section-title">📋 通知输入</div>', unsafe_allow_html=True)

    notification_text = st.text_area(
        "通知文本",
        value=st.session_state.input_text_val,
        height=240,
        placeholder="在此粘贴通知、公告、邮件、群聊消息…",
        label_visibility="collapsed",
        key="ta_input",
        on_change=_on_input_change,
    )

    btn1, btn2 = st.columns(2)
    with btn1:
        if st.button("🔍 识别通知", use_container_width=True, type="primary"):
            if not st.session_state.input_text_val.strip():
                st.warning("请先粘贴通知文本")
            else:
                with st.spinner("AI 正在识别…"):
                    try:
                        result = parse_notification(st.session_state.input_text_val)
                        st.session_state.preview_items = result
                        if not result:
                            st.info("未识别到日程或待办事项")
                    except Exception as e:
                        st.error(f"识别失败: {e}")
                st.rerun()

    with btn2:
        st.button(
            "清空输入",
            use_container_width=True,
            on_click=clear_input,
        )

    # ── Preview area ──
    if st.session_state.preview_items:
        st.divider()
        st.markdown('<div class="section-title">👀 识别预览</div>', unsafe_allow_html=True)
        st.caption("请确认以下内容，确认后才会保存")

        for i, item in enumerate(st.session_state.preview_items):
            render_item_card(item, extra="preview-card")

            col_t, col_p = st.columns([3, 1])
            with col_t:
                item.title = st.text_input(
                    "标题",
                    value=item.title,
                    key=f"pv_title_{i}",
                    label_visibility="collapsed",
                )
            with col_p:
                priorities = ["low", "medium", "high"]
                try:
                    pri_idx = priorities.index(item.priority)
                except ValueError:
                    pri_idx = 1
                item.priority = st.selectbox(
                    "优先级",
                    options=priorities,
                    index=pri_idx,
                    format_func=lambda v: {"low": "低", "medium": "中", "high": "高"}.get(v, v),
                    key=f"pv_pri_{i}",
                    label_visibility="collapsed",
                )

            if item.type == "event":
                col_d, col_s, col_e, col_l = st.columns([1.5, 0.8, 0.8, 1.5])
                with col_d:
                    d = _parse_date(item.date) or today
                    item.date = st.date_input(
                        "日期",
                        value=d,
                        key=f"pv_date_{i}",
                        label_visibility="collapsed",
                    ).isoformat()
                with col_s:
                    item.start_time = st.text_input(
                        "开始",
                        value=item.start_time or "",
                        key=f"pv_st_{i}",
                        label_visibility="collapsed",
                        placeholder="HH:MM",
                    ) or None
                with col_e:
                    item.end_time = st.text_input(
                        "结束",
                        value=item.end_time or "",
                        key=f"pv_et_{i}",
                        label_visibility="collapsed",
                        placeholder="HH:MM",
                    ) or None
                with col_l:
                    item.location = st.text_input(
                        "地点",
                        value=item.location or "",
                        key=f"pv_loc_{i}",
                        label_visibility="collapsed",
                        placeholder="地点",
                    ) or None
            else:
                col_dl, col_l2 = st.columns([2, 2])
                with col_dl:
                    item.deadline = st.text_input(
                        "截止时间",
                        value=item.deadline or "",
                        key=f"pv_dl_{i}",
                        label_visibility="collapsed",
                        placeholder="YYYY-MM-DD HH:MM",
                    ) or None
                with col_l2:
                    item.location = st.text_input(
                        "地点",
                        value=item.location or "",
                        key=f"pv_loc_{i}",
                        label_visibility="collapsed",
                        placeholder="地点",
                    ) or None

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

            col_del1, _ = st.columns([1.5, 8.5])
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
                        col_del_w, col_src, _ = st.columns([1.2, 1.2, 3.6])
                        with col_del_w:
                            if st.button("✕", key=f"del_w_{event.id}", help="删除"):
                                delete_event(event.id)
                                refresh_data()
                                st.rerun()
                        with col_src:
                            st.button("📋", key=f"src_w_{event.id}", help="查看原文", on_click=select_source, args=(event,))
                else:
                    st.markdown(
                        '<div style="text-align:center;color:#ddd;font-size:11px;padding:8px;">—</div>',
                        unsafe_allow_html=True,
                    )
    else:
        st.markdown(
            '<div class="empty-state">该周暂无其他日程</div>', unsafe_allow_html=True
        )

    # ── Source detail panel ──
    if st.session_state.selected_source_item is not None:
        selected = st.session_state.selected_source_item
        st.divider()
        st.markdown('<div class="section-title" style="color:#6366f1;">📋 原文详情</div>', unsafe_allow_html=True)
        st.markdown(_card_html(selected), unsafe_allow_html=True)
        st.caption("原文")
        st.markdown(
            f'<div class="source-full">{html.escape(selected.source_text)}</div>',
            unsafe_allow_html=True,
        )
        if st.button("关闭", key="close_source_detail"):
            st.session_state.selected_source_item = None
            st.rerun()

    # ── Unscheduled ──
    truly_unscheduled = [
        e for e in all_events if e.date is None
    ]
    truly_unscheduled.sort(key=lambda e: (e.priority != "high", e.priority != "medium", e.title))

    if truly_unscheduled:
        st.divider()
        st.markdown(
            '<div class="section-title" style="color:#f59e0b;">⚠️ 待确认 / 未安排</div>',
            unsafe_allow_html=True,
        )
        st.caption("这些日程日期不明确，需要手动确认")

        for event in truly_unscheduled:
            render_item_card(event)

            col_date, col_set, col_del3 = st.columns([2.5, 1.5, 1])
            with col_date:
                new_date = st.date_input(
                    "日期",
                    value=today,
                    key=f"date_{event.id}",
                    label_visibility="collapsed",
                )
            with col_set:
                if st.button("📅", key=f"set_date_{event.id}", help="设置日期"):
                    update_event(event.id, {"date": new_date.isoformat(), "needs_confirmation": "FALSE"})
                    refresh_data()
                    st.rerun()
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

            col_done, col_del3t, _ = st.columns([1.2, 1.2, 7.6])
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
            col_undo, col_del4, _ = st.columns([1.2, 1.2, 7.6])
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
