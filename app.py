from __future__ import annotations

import html
from datetime import date, timedelta
from typing import List, Optional

import streamlit as st

from ai_parser import parse_notification
from models import ScheduleItem
from d1_storage import delete_event, delete_todo, load_events, load_todos, save_event, save_todo, toggle_todo, update_event, update_todo

# 同一个模块再绑一个名字：A7 的诊断通道用 getattr 探测，函数缺失/改名时降级而不是崩溃
# 换存储层时这里要跟着改，并且用 `as sheets_storage` 保留旧名，
# 好让下面 388/414/518/531 四处 get_diagnostics / SKIPPED_MARKER /
# get_last_save_outcome 的引用一行都不用改。
import d1_storage as sheets_storage

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
    @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20,400,0,0&display=swap');

    html, body, [class*="st-"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    .material-icons,
    .material-symbols,
    .material-symbols-outlined,
    .material-symbols-rounded,
    .material-symbols-sharp,
    [data-testid="stIconMaterial"] {
        font-family: 'Material Symbols Rounded' !important;
        font-weight: normal !important;
        font-style: normal !important;
        font-size: 18px !important;
        line-height: 1 !important;
        letter-spacing: normal !important;
        text-transform: none !important;
        display: inline-block !important;
        white-space: nowrap !important;
        word-wrap: normal !important;
        direction: ltr !important;
        -webkit-font-feature-settings: 'liga' !important;
        -webkit-font-smoothing: antialiased !important;
        font-feature-settings: 'liga' !important;
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
    [data-testid="stPopover"] > details > summary {
        list-style: none;
    }
    [data-testid="stPopover"] > details > summary::-webkit-details-marker {
        display: none;
    }
    [data-testid="stPopover"] > details > summary::marker {
        content: "";
    }
    [data-testid="stPopover"] [data-testid="stIconMaterial"] {
        display: none !important;
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
        .week-grid > [data-testid="stHorizontalBlock"] {
            flex-wrap: nowrap !important;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            scroll-snap-type: x mandatory;
        }
        .week-grid > [data-testid="stHorizontalBlock"] > [data-testid="column"] {
            flex: 0 0 85vw !important;
            max-width: 85vw !important;
            scroll-snap-align: start;
        }
    </style>
    """,
        unsafe_allow_html=True,
    )

# ── auth ──────────────────────────────────────────────────────────────────────
ACCOUNTS = {"7X": "123456", "Jasper": "888888888", "lanmao": "97952", "RBumaro": "Anan_1122"}

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
else:
    # ── 存储层错误边界（R4）────────────────────────────────────────────────────
    # 登录后的首次加载、以及保存/删除/编辑后的 refresh_data 都必须经过这里：
    # 表格 404 / service account 配错 / 限流时给出可读中文提示并让页面继续用，
    # 而不是让一条裸 traceback 把整页顶掉（HEAD 上就是这样）。
    #
    # 存 session_state 而不是本轮局部：保存/删除之后紧跟的是 st.rerun()，那一轮
    # 的页面会被丢弃，局部 list 会跟着一起丢，错误就永远显示不出来。渲染完再清空。
    if "_load_errors" not in st.session_state:
        st.session_state["_load_errors"] = []
    _load_errors: List[str] = st.session_state["_load_errors"]


    def _safe_load(loader, user: str, what: str) -> list:
        try:
            return list(loader(user) or [])
        except Exception as e:
            _load_errors.append(f"读取{what}失败（{type(e).__name__}: {e}）")
            return []


    # ── session state init ───────────────────────────────────────────────────────
    if "pc_mode" not in st.session_state:
        st.session_state.pc_mode = False
    if "events" not in st.session_state:
        st.session_state.events = _safe_load(load_events, st.session_state.user, "日程")
    if "todos" not in st.session_state:
        st.session_state.todos = _safe_load(load_todos, st.session_state.user, "待办")
    if "preview_items" not in st.session_state:
        st.session_state.preview_items: List[ScheduleItem] = []
    if "input_text_val" not in st.session_state:
        st.session_state.input_text_val = ""
    if "selected_source_item" not in st.session_state:
        st.session_state.selected_source_item: Optional[ScheduleItem] = None


    def refresh_data(user: str) -> None:
        """R4 — 读失败不再抛到页面。

        调用方（保存 / 编辑 / 删除 / 勾选）的写入已经完成了，读只是刷新视图：
        读不通就保留上一轮的 session 数据、记一条提示，让"已经写成功"那部分
        照常如实报告，而不是把一次成功的保存反过来说成失败。
        """
        try:
            st.session_state.events = load_events(user)
        except Exception as e:
            _load_errors.append(f"刷新日程失败（{type(e).__name__}: {e}）")
        try:
            st.session_state.todos = load_todos(user)
        except Exception as e:
            _load_errors.append(f"刷新待办失败（{type(e).__name__}: {e}）")


    # A5 — 表里可能残留 id 重复的历史脏行（sheets_storage 的幂等保护只防"新增"重复），
    # 而 st.form 和带 key 的控件要求同一轮内 key 全局唯一，撞了就抛 DuplicateWidgetID /
    # StreamlitAPIException，整页打不开。按渲染顺序给重复项追加序号：首个仍用原 key，
    # 第 2/3 个用 key__2 / key__3…
    _KEY_SEQ: dict = {}


    def _uniq_key(key: str) -> str:
        n = _KEY_SEQ.get(key, 0)
        _KEY_SEQ[key] = n + 1
        return key if n == 0 else f"{key}__{n + 1}"


    # A3/A4 — LLM 控制的文本进入 unsafe_allow_html / markdown 之前必须转义。
    # 注意 st.markdown(..., unsafe_allow_html=True) 仍然会先跑一遍 markdown 解析器，
    # 只做 html.escape 挡不住 ![](url)（会变成真 <img> 去请求远端），所以两条通道都要处理。
    _MD_SPECIAL = set("\\`*_[]()!~")          # markdown 构造：图片/链接/代码/强调/转义符


    def _md_escape(text: str) -> str:
        """markdown 汇（st.caption / st.warning）：原文仍可见，但 ![](url)、`<script>` 之类失效。"""
        special = _MD_SPECIAL | set("<>")
        return "".join("\\" + ch if ch in special else ch for ch in str(text))


    def _safe_html(value) -> str:
        """unsafe_allow_html 汇：先掐掉 markdown 构造（不含 < >，留给 html.escape），再转义 HTML。"""
        escaped = "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in str(value if value is not None else ""))
        return html.escape(escaped, quote=True)


    def _render_parse_warnings() -> None:
        """A7 — ai_parser 把它这一轮丢弃/降级的问题写进 st.session_state["parse_warnings"]
        （每次调用开头清空，抛异常时也照写）。这里渲染后立刻清空，避免下次交互重复出现。"""
        try:
            warnings = st.session_state.get("parse_warnings")
        except Exception:
            return
        if not isinstance(warnings, list) or not warnings:
            return
        for w in warnings:
            # 消息里可能夹带 AI 返回的原始片段，按 markdown 转义后再显示（A3 同源问题）
            st.warning(_md_escape(f"识别提示: {w}"))
        try:
            del warnings[:]  # 就地清空，保留 session_state 里同一个 list 对象
        except Exception:
            st.session_state["parse_warnings"] = []


    def _drain_storage_notices() -> tuple:
        """A7 — sheets_storage 把"写入未生效 / 删除未命中 / 脏行"等条件收在
        get_diagnostics() 里（不再画全页横幅）。取一次并按栏位分桶，渲染完调
        clear_diagnostics()。函数缺失/改名/抛异常一律降级为"没有提示"。"""
        ev, td = [], []
        try:
            get_diagnostics = getattr(sheets_storage, "get_diagnostics", None)
            if get_diagnostics is None:
                return ev, td
            notices = get_diagnostics()
        except Exception:
            return ev, td
        if not notices:
            return ev, td
        try:
            entries = list(notices.values()) if isinstance(notices, dict) else list(notices)
        except Exception:
            return ev, td
        for n in entries:
            if isinstance(n, dict):
                code = str(n.get("code", ""))
                msg = str(n.get("message", ""))
                count = n.get("count", 1)
            else:  # 旧版形状：直接就是字符串
                code, msg, count = "", str(n), 1
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = 1
            text = _md_escape(f"[{code}] {msg}" + (f"（×{count}）" if count > 1 else ""))
            (td if ("todo" in code or "待办" in msg) else ev).append(text)
        try:
            clear_diagnostics = getattr(sheets_storage, "clear_diagnostics", None)
            if clear_diagnostics is not None:
                clear_diagnostics()
        except Exception:
            pass
        return ev, td


    def _render_counter(slot) -> None:
        """把"X 条日程 · Y 条待办"计数行写进占位（见 A6：必须在本轮保存之后才调用）。"""
        events = st.session_state.events
        todos = st.session_state.todos
        event_count = len(events)
        all_todo_count = len(todos)
        completed_todo_count = sum(1 for t in todos if t.is_completed)
        active_todo_count = all_todo_count - completed_todo_count
        if event_count > 0 or all_todo_count > 0:
            slot.markdown(
                f'<p style="color:#bbb;font-size:12px;margin-bottom:16px;">{event_count} 条日程 · {active_todo_count} 条待办 · {completed_todo_count} 条已完成</p>',
                unsafe_allow_html=True,
            )
        else:
            slot.markdown('<p style="color:#bbb;font-size:12px;margin-bottom:16px;">暂无数据</p>', unsafe_allow_html=True)


    def clear_input() -> None:
        st.session_state.input_text_val = ""
        st.session_state["ta_input"] = ""
        st.session_state.preview_items = []


    def _on_input_change() -> None:
        st.session_state.input_text_val = st.session_state["ta_input"]


    def select_source(item: ScheduleItem) -> None:
        st.session_state.selected_source_item = item


    def _edit_popover(item: ScheduleItem, prefix: str = "") -> None:
        """Render an edit popover form for a schedule item."""
        pf = f"{prefix}_{item.id}" if prefix else item.id
        pf = _uniq_key(pf)
        with st.popover("✏️", help="编辑"):
            with st.form(f"edit_form_{pf}"):
                new_title = st.text_input("标题", value=item.title, key=f"edit_title_{pf}")
                priorities = ["low", "medium", "high"]
                try:
                    pri_idx = priorities.index(item.priority)
                except ValueError:
                    pri_idx = 1
                new_priority = st.selectbox(
                    "优先级", options=priorities, index=pri_idx,
                    format_func=lambda v: {"low": "低", "medium": "中", "high": "高"}.get(v, v),
                    key=f"edit_pri_{pf}",
                )
                if item.type == "event":
                    parsed_date = _parse_date(item.date)
                    d = parsed_date or today
                    if parsed_date is None:
                        st.caption("该日程尚未安排日期：只有在下方选定日期后才会保存日期")
                        # A1 — 未排期的日程用"空日期"起手（streamlit ≥1.50 的 value=None
                        # 渲染空状态并返回 None，直到用户主动选择）。若默认填"今天"，
                        # "没动过"和"选了今天"无法区分，选今天会被静默丢弃。
                        new_date = st.date_input("日期", value=None, key=f"edit_date_{pf}")
                    else:
                        new_date = st.date_input("日期", value=d, key=f"edit_date_{pf}")
                    new_start = st.text_input("开始时间", value=item.start_time or "", key=f"edit_st_{pf}")
                    new_end = st.text_input("结束时间", value=item.end_time or "", key=f"edit_et_{pf}")
                    new_location = st.text_input("地点", value=item.location or "", key=f"edit_loc_{pf}")
                else:
                    new_date = None
                    new_start = new_end = None
                    new_deadline = st.text_input("截止时间", value=item.deadline or "", key=f"edit_dl_{pf}")
                    new_location = st.text_input("地点", value=item.location or "", key=f"edit_loc_{pf}")

                if st.form_submit_button("保存修改", key=f"edit_save_{pf}"):
                    user = st.session_state.user
                    updates: dict = {"title": new_title, "priority": new_priority}
                    if item.type == "event":
                        # 已排期：只有日期真的改了才写回（避免把默认值当成改动）；
                        # 未排期：date_input 以空值起手，只要返回了日期就是用户主动选的，
                        # 哪怕正好是"今天"，也必须写进去。
                        if parsed_date is None:
                            if new_date is not None:
                                updates["date"] = new_date.isoformat()
                        elif new_date != d:
                            updates["date"] = new_date.isoformat()
                        updates["start_time"] = new_start or ""
                        updates["end_time"] = new_end or ""
                    else:
                        updates["deadline"] = new_deadline or ""
                    updates["location"] = new_location or ""
                    updates["needs_confirmation"] = "FALSE"
                    if item.type == "event":
                        update_event(item.id, updates)
                    else:
                        update_todo(item.id, updates)
                    refresh_data(user)
                    st.rerun()


    # ── helpers ──────────────────────────────────────────────────────────────────
    def _skip_marker() -> str:
        return str(getattr(sheets_storage, "SKIPPED_MARKER", "[skipped:duplicate-id]"))


    def _read_save_outcome(saved_range) -> dict:
        """R2 — 刚 save 完的这一条到底写进表了没有。

        sheets_storage 对"命中同 id、故意不追加"会留一份机器可读的
        get_last_save_outcome()（{"saved": False, "skipped": True, ...}），同时在
        返回字符串里带上内部标记。优先读前者；函数缺失 / 改名 / 抛异常时降级为
        看返回字符串里的标记。两者都拿不到明确信号时按"已写入"处理——宁可比实际
        多算一条，也不要把真的写成功的行说成没保存。
        """
        try:
            out = sheets_storage.get_last_save_outcome()
            if isinstance(out, dict) and (out.get("saved") or out.get("skipped")):
                skipped = bool(out.get("skipped")) or out.get("status") == "skipped"
                return {"saved": bool(out.get("saved")) and not skipped, "skipped": skipped}
        except Exception:
            pass
        text = str(saved_range or "")
        skipped = _skip_marker() in text or "[skipped:" in text
        return {"saved": not skipped, "skipped": skipped}


    def _clean_saved_range(value) -> str:
        """展示用的落点文本：内部标记 [skipped:…] 只面向程序，绝不出现在页面上。"""
        text = str(value or "").replace(_skip_marker(), "")
        idx = text.find("[skipped:")
        if idx != -1:
            end = text.find("]", idx)
            text = text[:idx] + (text[end + 1:] if end != -1 else "")
        return text.strip()


    def _parse_date(d: Optional[str]) -> Optional[date]:
        try:
            return date.fromisoformat(d) if d else None
        except (ValueError, TypeError):
            return None


    def _card_html(item: ScheduleItem, extra: str = "") -> str:
        priority_label = {"high": "高", "medium": "中", "low": "低"}.get(item.priority, "低")
        safe_title = _safe_html(item.title)
        source = _safe_html(item.source_text.replace("\n", " "))[:80]
        # priority / extra 会进入 class 属性：取不到白名单值就回落到 low，不原样插入
        priority_cls = item.priority if item.priority in ("low", "medium", "high") else "low"
        safe_extra = _safe_html(extra)

        if item.type == "event":
            time_parts = []
            if item.start_time:
                t = _safe_html(item.start_time)
                if item.end_time:
                    t += f" - {_safe_html(item.end_time)}"
                time_parts.append(t)
            elif item.time_period:
                period_labels = {"morning": "上午", "noon": "中午", "afternoon": "下午", "evening": "晚上", "night": "半夜"}
                time_parts.append(_safe_html(period_labels.get(item.time_period, item.time_period)))
            if item.location:
                time_parts.append(f"📍 {_safe_html(item.location)}")
            meta = " · ".join(time_parts) if time_parts else ""
        else:
            meta = ""
            if item.deadline:
                meta = f"截止: {_safe_html(item.deadline)}"
            if item.location:
                meta += f" · 📍 {_safe_html(item.location)}" if meta else f"📍 {_safe_html(item.location)}"

        confirm = ""
        if item.needs_confirmation:
            confirm = '<span class="confirm-badge">需确认</span>'

        return f"""
        <div class="card {safe_extra}">
            <div class="card-title">{safe_title} {confirm} <span class="badge badge-{priority_cls}">{priority_label}</span></div>
            <div class="card-meta">{meta}</div>
            <div class="card-source">原文: {source}</div>
        </div>
        """


    def _compact_card_html(item: ScheduleItem) -> str:
        priority_color = {"high": "#ef4444", "medium": "#f59e0b", "low": "#9ca3af"}
        color = priority_color.get(item.priority, "#9ca3af")
        safe_title = _safe_html(item.title)

        time_str = ""
        if item.start_time:
            time_str = _safe_html(item.start_time)
            if item.end_time:
                time_str += f"-{_safe_html(item.end_time)}"
        elif item.time_period:
            period_labels = {"morning": "上午", "noon": "中午", "afternoon": "下午", "evening": "晚上", "night": "半夜"}
            time_str = _safe_html(period_labels.get(item.time_period, item.time_period or ""))

        meta_parts = [time_str] if time_str else []
        if item.location:
            meta_parts.append(_safe_html(item.location))
        meta = " · ".join(meta_parts)

        confirm = '<span style="background:#fef2f2;color:#dc2626;font-size:9px;padding:0 3px;border-radius:2px;">!</span>' if item.needs_confirmation else ""

        return f"""
        <div class="compact-card" style="background:#fff;border-radius:6px;padding:6px 8px;margin-bottom:5px;border-left:2px solid {color};font-size:11px;line-height:1.4;">
            <div style="font-weight:600;color:#1a1a1a;">{safe_title} {confirm}</div>
            <div style="color:#888;font-size:10px;">{meta}</div>
        </div>
        """


    def _source_details_html(card_html: str, source_text: str) -> str:
        safe_source = _safe_html(source_text)
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
            f'<span style="color:#6366f1;font-weight:600;">👤 {_safe_html(st.session_state.user)}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if st.button("退出", key="logout_btn"):
            # 必须删除全部会话级 key（含 user 本身），否则下一个用户会命中 279-290 的
            # `not in session_state` 守卫而不重新加载数据，并看到上一个用户的草稿
            for _k in ("user", "events", "todos", "preview_items", "input_text_val",
                       "ta_input", "selected_source_item", "pc_mode", "_pending_ta_reset",
                       "_pending_ta_reset_text", "_load_errors"):
                if _k in st.session_state:
                    del st.session_state[_k]
            st.rerun()
    with col_mode_btn:
        mode_label = "🖥️" if st.session_state.pc_mode else "📱"
        mode_help = "切换为PC版" if not st.session_state.pc_mode else "切换为手机版"
        if st.button(mode_label, key="toggle_pc_mode", help=mode_help):
            st.session_state.pc_mode = not st.session_state.pc_mode
            st.rerun()

    # A6 — 计数行依赖 session 里的数据，而"确认保存"是在本轮页面下半部分才写入数据的
    # （st.rerun() 会把刚打印的"已保存 N 条"冲掉）。所以这里只放一个占位，等整页渲染完
    # 再用最终数据填进去，保证同一轮里计数就是对的。
    _counter_slot = st.empty()

    # R4 — 存储层读不通时的横幅。容器开在页面最上面，内容等整页跑完再填：加载失败
    # 发生在开头，保存/删除后的 refresh 失败发生在下半页，只在开头打印的话后者会漏。
    _error_slot = st.container()

    # A7 — 存储层诊断取一次，按栏位就地渲染（见 _drain_storage_notices）
    _ev_notices, _todo_notices = _drain_storage_notices()

    # R3 — 保存之后还要再排空一次（保存自己也会产生诊断），本轮只允许补排一次
    _notices_drained_again = False

    # ── R1：重复 id 的历史脏行 ─────────────────────────────────────────────────
    # 重复 id 的卡片上固定显示的这一行（见 _is_duplicate 的用法处）。
    _DUP_CAPTION = "⚠️ 重复记录：同一个 id 在表里出现了多次，无法确定改/删的是哪一行，此处只读（详见上方提示）"

    # 存储 API 是"按 id 命中第一行"的（update_event / delete_event / toggle_todo），
    # 而表里可能残留同 id 的多行（幂等保护只挡"新增"重复，挡不住历史脏数据）。
    # 上一轮用 _uniq_key 给重复项拼了唯一 key，消掉了 DuplicateWidgetID 崩溃，
    # 却让"第 2 个副本"的删除/编辑静默打到第 1 行上——比崩溃更糟。
    # 重复 id 本身就是坏数据，无法安全地定位到某一行，所以本轮采取的策略是：
    # 同一 id 的**每一份副本**都只读渲染（不出现编辑/删除/勾选/原文按钮），
    # 卡片仍然可见，并给出提示。namespace 是 type（应用层按 type 分派 event/todo
    # 存储 API），所以同 id 的 event 与 todo 互不牵连。
    def _dup_id_keys(items) -> set:
        seen, dups = set(), set()
        for it in items:
            key = (it.type, it.id)
            if key in seen:
                dups.add(key)
            seen.add(key)
        return dups


    _dup_keys = _dup_id_keys(st.session_state.events) | _dup_id_keys(st.session_state.todos)


    def _is_duplicate(item) -> bool:
        return (item.type, item.id) in _dup_keys


    def _dup_notice(items, label: str) -> Optional[str]:
        """把重复 id 汇总成一条栏位提示（走 A7 已有的 _ev_notices / _todo_notices 通道）。"""
        ids = sorted({i.id for i in items if _is_duplicate(i) and i.id})
        if not ids:
            return None
        shown = ", ".join(ids[:5]) + ("…" if len(ids) > 5 else "")
        return _md_escape(
            f"⚠️ 检测到 {len(ids)} 个 id 重复的{label}记录（{shown}）："
            "同 id 的记录无法区分是哪一行，改/删都会打到第一行，"
            "已全部设为只读，请在表格里手工删掉多余行后刷新页面"
        )


    _dup_ev_notice = _dup_notice(st.session_state.events, "日程")
    if _dup_ev_notice:
        _ev_notices.append(_dup_ev_notice)
    _dup_todo_notice = _dup_notice(st.session_state.todos, "待办")
    if _dup_todo_notice:
        _todo_notices.append(_dup_todo_notice)

    today = date.today()

    # ── 3-column layout ──────────────────────────────────────────────────────────
    col_input, col_events, col_todos = st.columns([1, 1.2, 0.8])

    # ══════════════════════════════════════════════════════════════════════════════
    # LEFT: Input + Preview
    # ══════════════════════════════════════════════════════════════════════════════
    with col_input:
        st.markdown('<div class="section-title">📋 通知输入</div>', unsafe_allow_html=True)

        # 保存成功后延迟清空输入框：必须在本轮 ta_input 实例化之前赋值。
        # 只清"保存时那一版"文本——用户可能在下一轮之前已经粘了新的通知，
        # 无条件清空会把新文本吃掉（on_change 已把它写进 input_text_val，
        # 而输入框显示为空，识别通知就会去解析看不见的内容）。
        #
        # R5 — 清空在这里同时做两件事（控件值 ta_input 和解析用的 input_text_val）。
        # 之前是在保存那一轮先把 input_text_val 清了、ta_input 留到下一轮才清，
        # 于是中间那一轮"框里还显示着通知、解析值却是空的"，用户点"识别通知"
        # 只会得到"请先粘贴通知文本"。现在两边永远同一轮一起清，不会再分叉。
        _ta_cleared_by_save = False
        if st.session_state.get("_pending_ta_reset"):
            st.session_state["_pending_ta_reset"] = False
            if "_pending_ta_reset_text" in st.session_state:
                saved_text = st.session_state["_pending_ta_reset_text"]
                del st.session_state["_pending_ta_reset_text"]
            else:
                saved_text = None
            if "ta_input" in st.session_state and (
                saved_text is None or st.session_state["ta_input"] == saved_text
            ):
                st.session_state["ta_input"] = ""
                st.session_state.input_text_val = ""
                _ta_cleared_by_save = True

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
                    if _ta_cleared_by_save:
                        # R5 — 刚才那批已经存进表格了、输入框是被保存动作清空的，
                        # 说"请先粘贴"会让人以为通知丢了。如实说明去哪儿了。
                        st.info("上一条通知已保存到表格，输入框已自动清空；如需再次识别请重新粘贴")
                    else:
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
                    # A7 — 被丢弃/降级的条目在这里解释，渲染后即清空
                    _render_parse_warnings()
                    # 不在此处 st.rerun()：重跑会清空本次运行刚输出的提示信息，
                    # preview_items 已在本轮 559 行之后直接渲染

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
                col_card, col_del = st.columns([10, 1])
                with col_card:
                    render_item_card(item, extra="preview-card")
                with col_del:
                    if st.button("✕", key=f"pv_del_{i}", help="移除此条"):
                        st.session_state.preview_items.pop(i)
                        st.rerun()

            with st.form("preview_form"):
                submitted = st.form_submit_button("✅ 确认保存", use_container_width=True, type="primary")

            if submitted:
                user = st.session_state.user
                saved_ranges = []
                failed = []
                remaining = []
                skipped_items = []
                # 逐条保存：不因单条失败而中断，成功的移出 preview_items（避免重试时重复写入）
                for item in st.session_state.preview_items:
                    try:
                        if item.type == "event":
                            saved_range = save_event(item, user)
                        elif item.type == "todo":
                            saved_range = save_todo(item, user)
                        else:
                            raise ValueError(f"未知类型: {item.type}")
                    except Exception as e:
                        failed.append((item, str(e)))
                        remaining.append(item)
                        continue
                    # R2 — get_last_save_outcome 是"每条"状态，必须紧跟在这一条的
                    # save 之后读；读到的下一条就会覆盖它。
                    if _read_save_outcome(saved_range)["skipped"]:
                        # 表里已经有同 id 的未删除记录 → 视为"已经在库里"：
                        # 不计入"已保存 N 条"，也不退回 preview_items（否则每次点确认
                        # 都会被再次去重，永远清不掉），但会明确告诉用户没新写入。
                        skipped_items.append(item)
                        continue
                    saved_ranges.append(_clean_saved_range(saved_range))
                st.session_state.preview_items = remaining
                refresh_data(user)

                saved_locations = [str(saved_range) for saved_range in saved_ranges if saved_range]
                location_text = f": {', '.join(saved_locations)}" if saved_locations else ""
                for item, err in failed:
                    st.error(f"保存失败: {item.title} → {err}")
                if skipped_items:
                    st.info(
                        f"{len(skipped_items)} 条已存在于表格中，未重复保存: "
                        + ", ".join(_md_escape(i.title) for i in skipped_items[:3])
                        + ("…" if len(skipped_items) > 3 else "")
                    )
                if failed:
                    st.warning(
                        f"已保存 {len(saved_ranges)} 条，失败 {len(failed)} 条"
                        f"{location_text}（失败项已保留在上方预览中，可修改后重新保存）"
                    )
                elif not saved_ranges:
                    st.info("没有新记录写入表格")
                else:
                    st.success(f"已保存 {len(saved_ranges)} 条记录{location_text}")
                    # R5 — 不在这里清 input_text_val：那样会出现"框里还显示着通知、
                    # 解析值已经是空"的一轮。现在只置标记，由下一轮开头那段延迟重置
                    # 同时清 ta_input 和 input_text_val，两者永远一致。
                    # 记录保存时的原文，下一轮据此判断"是不是用户已经改过/粘过新内容了"
                    st.session_state["_pending_ta_reset_text"] = notification_text
                    st.session_state["_pending_ta_reset"] = True

            # R3 — 保存本身也会产生存储层诊断（写入未生效 / 去重跳过 / 脏行），
            # 而本轮开头那次排空发生在保存之前，提示要等到下一次交互才显示得到。
            # 这里在保存之后再排空一次，追加到各栏位的提示队列里。
            if not _notices_drained_again:
                _notices_drained_again = True
                _ev2, _td2 = _drain_storage_notices()
                _ev_notices.extend(_ev2)
                _todo_notices.extend(_td2)

            if st.button("取消", use_container_width=True):
                st.session_state.preview_items = []
                st.rerun()

    # ══════════════════════════════════════════════════════════════════════════════
    # MIDDLE: Schedule
    # ══════════════════════════════════════════════════════════════════════════════
    with col_events:
        all_events = st.session_state.events

        # A7 — 存储层诊断就地显示在日程栏（不画全页横幅）
        for _n in _ev_notices:
            st.warning(_n)

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

                if _is_duplicate(event):          # R1 — 重复 id：只读渲染，不给任何写入口
                    st.caption(_DUP_CAPTION)
                else:
                    col_edit_t, col_del_t, _ = st.columns([1, 1, 8])
                    with col_edit_t:
                        _edit_popover(event, "today")
                    with col_del_t:
                        if st.button("🗑", key=_uniq_key(f"del_today_{event.id}"), help="删除"):
                            delete_event(event.id)
                            refresh_data(st.session_state.user)
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
            st.markdown('<div class="week-grid">', unsafe_allow_html=True)
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
                            render_item_card(event, compact=False, show_source=False)
                            if _is_duplicate(event):   # R1 — 重复 id：只读渲染
                                st.caption(_DUP_CAPTION)
                            else:
                                col_edit_w, col_del_w, col_src, _ = st.columns([1, 1, 1, 3])
                                with col_edit_w:
                                    _edit_popover(event, "week")
                                with col_del_w:
                                    if st.button("✕", key=_uniq_key(f"del_w_{event.id}"), help="删除"):
                                        delete_event(event.id)
                                        refresh_data(st.session_state.user)
                                        st.rerun()
                                with col_src:
                                    st.button("📋", key=_uniq_key(f"src_w_{event.id}"), help="查看原文", on_click=select_source, args=(event,))
                    else:
                        st.markdown(
                            '<div style="text-align:center;color:#ddd;font-size:11px;padding:8px;">—</div>',
                            unsafe_allow_html=True,
                        )
            st.markdown('</div>', unsafe_allow_html=True)
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
                f'<div class="source-full">{_safe_html(selected.source_text)}</div>',
                unsafe_allow_html=True,
            )
            if st.button("关闭", key="close_source_detail"):
                st.session_state.selected_source_item = None
                st.rerun()

        # ── Unscheduled ──
        # 日期为空、或日期字符串无法被识别(如 "下周三" / "2026/10/02")的日程，
        # 否则它们既进不了今日/周日程，也会被永久隐藏
        truly_unscheduled = [
            e for e in all_events
            if e.date is None or (e.date and _parse_date(e.date) is None)
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
                if event.date and _parse_date(event.date) is None:
                    # A4 — st.caption 会把字符串当 markdown 渲染，未转义的 ![](url) 会触发
                    # 远程请求；原始值仍然可见，但只是纯文本
                    st.caption(f"原始日期「{_md_escape(event.date)}」无法识别，请用 ✏️ 重新选择日期")

                if _is_duplicate(event):          # R1 — 重复 id：只读渲染，不给任何写入口
                    st.caption(_DUP_CAPTION)
                else:
                    col_edit_u, col_del_u, _ = st.columns([1, 1, 8])
                    with col_edit_u:
                        _edit_popover(event, "unsched")
                    with col_del_u:
                        if st.button("🗑", key=_uniq_key(f"del_unsched_{event.id}"), help="删除"):
                            delete_event(event.id)
                            refresh_data(st.session_state.user)
                            st.rerun()

    # ══════════════════════════════════════════════════════════════════════════════
    # RIGHT: Todos
    # ══════════════════════════════════════════════════════════════════════════════
    with col_todos:
        st.markdown('<div class="section-title">📝 待办事项</div>', unsafe_allow_html=True)

        # A7 — 待办相关的存储层诊断就地显示在待办栏
        for _n in _todo_notices:
            st.warning(_n)

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

                if _is_duplicate(todo):           # R1 — 重复 id：只读渲染
                    st.caption(_DUP_CAPTION)
                else:
                    col_edit_td, col_done, col_del_td, _ = st.columns([1, 1, 1, 7])
                    with col_edit_td:
                        _edit_popover(todo, "todo")
                    with col_done:
                        if st.button("✅", key=_uniq_key(f"done_{todo.id}"), help="标记完成"):
                            toggle_todo(todo.id)
                            refresh_data(st.session_state.user)
                            st.rerun()
                    with col_del_td:
                        if st.button("🗑", key=_uniq_key(f"del_todo_{todo.id}"), help="删除"):
                            delete_todo(todo.id)
                            refresh_data(st.session_state.user)
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
                if _is_duplicate(todo):           # R1 — 重复 id：只读渲染
                    st.caption(_DUP_CAPTION)
                    continue
                col_undo, col_del4, _ = st.columns([1.2, 1.2, 7.6])
                with col_undo:
                    if st.button("↩", key=_uniq_key(f"undo_{todo.id}"), help="恢复未完成"):
                        toggle_todo(todo.id)
                        refresh_data(st.session_state.user)
                        st.rerun()
                with col_del4:
                    if st.button("🗑", key=_uniq_key(f"del_done_{todo.id}"), help="删除"):
                        delete_todo(todo.id)
                        refresh_data(st.session_state.user)
                        st.rerun()

    # A6 — 整页渲染完成后用最终数据填计数行（本轮保存过就是本轮的条数，不等下一次交互）
    _render_counter(_counter_slot)

    # R4 — 同上，存储层读不通的横幅也用最终这一轮的结果来填（渲染完即清空，
    # 避免同一个错误在以后每一轮都重复刷屏）
    if _load_errors:
        with _error_slot:
            for _e in _load_errors:
                st.error(f"⚠️ {_e}　（下方看板可能是空的或过期的，稍后再点一次任意操作会重试）")
        _load_errors.clear()
