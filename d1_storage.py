"""D1 存储后端 —— `sheets_storage.py` 的 drop-in 替身。

这个模块刻意与 `sheets_storage.py` 保持**完全相同的公开接口**：
`load_events` / `load_todos` / `save_event` / `save_todo` / `delete_event` /
`delete_todo` / `toggle_todo` / `update_event` / `update_todo` /
`get_diagnostics` / `clear_diagnostics` / `get_last_save_outcome` /
`SKIPPED_MARKER` / `LEGACY_USER_OWNER` / `EVENTS_HEADERS`。

切换方式只有一行：

    # app.py
    - from sheets_storage import ...
    + from d1_storage import ...

`app.py` 里其他任何代码都不用改——包括它对 `get_last_save_outcome()`、
`SKIPPED_MARKER`、`get_diagnostics()` 的消费逻辑，两者语义已逐条对齐。

配置（三选一，按顺序）：
    1. Streamlit secrets：  WORKER_URL / WORKER_TOKEN
    2. 环境变量：           WORKER_URL / WORKER_TOKEN
    3. 本文件顶部的硬编码常量（仅本地调试用，不建议）

关于归属策略：user 为空的"无归属行"全员可读；Worker 端
`LEGACY_USER_OWNER` 为空时无人可写。语义与 sheets_storage 完全一致。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

import streamlit as st

from models import ScheduleItem

logger = logging.getLogger(__name__)

# ── 配置 ────────────────────────────────────────────────────────────────────
# 本地调试可临时填这里；正式请用 secrets.toml 或环境变量。
FALLBACK_WORKER_URL = ""
FALLBACK_WORKER_TOKEN = ""

TIMEOUT_S = 15

# 与 sheets_storage.EVENTS_HEADERS 保持一致（迁移/排查时用得上）。
EVENTS_HEADERS = [
    "id", "type", "title",
    "date", "start_time", "end_time",
    "deadline", "location",
    "priority", "source_text",
    "confidence", "needs_confirmation",
    "completed", "deleted",
    "created_at", "updated_at",
    "time_period",
    "user",
]

# 写入被去重跳过时返回串里必须含这个标记，页面据此区分"存了"和"没存"。
# 改这个值前请同步 d1/worker.js 里的同名常量。
SKIPPED_MARKER = "[skipped:duplicate-id]"

# 无归属行的托管账号：None = 谁都不能改。
# 真正的开关在 Worker 端（wrangler.toml 的 LEGACY_USER_OWNER），
# 并且**会比对调用者是谁**——托管人只能操作自己那一份。
# 改这个 Python 常量没有任何效果，它只是本地留档。
LEGACY_USER_OWNER: Optional[str] = None

# 最近一次 load 用过的账号。**只作为 st.session_state 读不到时的兜底**——
# 真正的身份来源是每个会话各自的 st.session_state.user。
#
# 早先这里只有模块级变量，两个账号同时登录会互相踩：谁后加载谁就顶掉
# 前一个的身份，导致前一个的删除/编辑/勾选全部被静默拒绝。
# Streamlit Cloud 一个进程跑很多会话，模块级变量是共享的，绝不能拿它当身份。
_last_loaded_user: str = ""


def _acting_user() -> str:
    """当前这一下操作是谁在点。

    优先读 st.session_state.user —— 那是每个浏览器会话独立的一份，
    天然隔离。读不到（非 Streamlit 场景：迁移脚本、单元测试）才退回
    最近一次 load 记下的值。
    """
    try:
        u = st.session_state.get("user")
    except Exception:
        u = None
    if u and str(u).strip():
        return str(u).strip()
    return _last_loaded_user

_PRIORITIES = {"low", "medium", "high"}
_TIME_PERIODS = {"morning", "noon", "afternoon", "evening", "night"}

# ── 诊断缓冲（结构与 sheets_storage 一致）────────────────────────────────────
# {code: {code, level, count, message, first_seen}}
_notices: List[Dict[str, Any]] = []
_notice_counts: Dict[str, int] = {}
MAX_NOTICES = 50
LOG_EACH_CODE_UPTO = 3

# ── 最近一次保存的结果（每条写入后立即读取，不能跨条复用）──────────────────
_last_save_outcome: Dict[str, Any] = {
    "status": "none",       # "none" | "saved" | "skipped" | "error"
    "saved": False,         # 仅当确实插入了新行才为 True
    "skipped": False,
    "sheet": "",            # "events" | "todos"
    "item_id": "",
    "range": "",
    "detail": "",
}


def _notice(code: str, message: str, level: str = "warning") -> None:
    """记录一条机器可读的通知；不画全页横幅，页面通过 get_diagnostics 拉取。"""
    count = _notice_counts.get(code, 0) + 1
    _notice_counts[code] = count
    if count <= LOG_EACH_CODE_UPTO:
        getattr(logger, level, logger.warning)("[d1] %s: %s", code, message)
    for n in reversed(_notices):
        if n["code"] == code:
            n["count"] = count
            n["message"] = message
            break
    else:
        if len(_notices) >= MAX_NOTICES:
            _notices.pop(0)
        _notices.append({
            "code": code,
            "level": level,
            "count": count,
            "message": message,
            "first_seen": datetime.now().isoformat(timespec="seconds"),
        })


def get_diagnostics() -> Dict[str, Dict[str, Any]]:
    """返回 {code: {...}} 的副本。app.py 用它渲染栏位提示后调用 clear_diagnostics()。"""
    return {n["code"]: dict(n) for n in _notices}


def clear_diagnostics() -> None:
    _notices.clear()
    _notice_counts.clear()


def get_last_save_outcome() -> Dict[str, Any]:
    """最近一次 save_event/save_todo 的结果。批量保存时必须每条之后立刻读。"""
    return dict(_last_save_outcome)


def _record_save_outcome(status: str, sheet: str, item: ScheduleItem,
                         range_: str = "", detail: str = "") -> Dict[str, Any]:
    _last_save_outcome.update({
        "status": status,
        "saved": status == "saved",
        "skipped": status == "skipped",
        "sheet": sheet,
        "item_id": item.id,
        "range": range_,
        "detail": detail,
    })
    try:
        return get_last_save_outcome()
    except Exception:  # pragma: no cover - 防御性：记账绝不能毁掉已完成的写入
        return dict(_last_save_outcome)


# ── 配置读取与 HTTP ─────────────────────────────────────────────────────────

def _config(name: str) -> str:
    val = ""
    try:
        val = st.secrets.get(name, "") or ""
    except Exception:
        val = ""
    if not val:
        val = os.environ.get(name, "") or ""
    if not val:
        val = FALLBACK_WORKER_URL if name == "WORKER_URL" else FALLBACK_WORKER_TOKEN
    return str(val).strip()


class _ConfigError(RuntimeError):
    pass


def _request(path: str, *, method: str = "GET",
             payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = _config("WORKER_URL")
    token = _config("WORKER_TOKEN")
    if not base or not token:
        raise _ConfigError(
            "未配置 WORKER_URL / WORKER_TOKEN。本地请写进 .streamlit/secrets.toml，"
            "Streamlit Cloud 请写进 App → Settings → Secrets。"
        )
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code == 401:
            raise _ConfigError("WORKER_TOKEN 不正确，请核对 secrets 里的值。") from e
        raise RuntimeError(f"Worker 返回 HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上 Worker（{e.reason}），请检查 WORKER_URL 与网络。") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Worker 返回的不是合法 JSON: {e}") from e


def _absorb_notices(resp: Dict[str, Any]) -> set:
    """把 Worker 带回的通知并入本地诊断缓冲。

    返回 Worker 已经报过的 code 集合——调用方据此**不要**再补记同一条，
    否则一次「没找到」会被记成两次，页面上就会显示（×2）。
    """
    seen = set()
    for n in (resp.get("notices") or []):
        if isinstance(n, dict) and n.get("code"):
            code = str(n["code"])
            seen.add(code)
            _notice(code, str(n.get("message", "")), str(n.get("level", "warning")))
    return seen


# ── 行 → ScheduleItem ───────────────────────────────────────────────────────

def _safe_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v or "").strip().lower() in ("true", "1", "yes")


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _safe_choice(raw: Any, allowed: set, fallback: Optional[str]) -> Optional[str]:
    """归一化枚举字段：非法值退到默认值，绝不让整行校验失败。

    折大小写，和 sheets_storage._safe_choice 保持一致——否则 priority="HIGH"
    在这里会静默退成 medium，徽章颜色就错了。
    """
    s = str(raw if raw is not None else "").strip().lower()
    return s if s in allowed else fallback


def _row_to_item(row: Dict[str, Any], default_type: str = "event") -> Optional[dict]:
    """D1 行 → Pydantic 字段字典。返回 None 表示这行应被跳过。"""
    row_id = str(row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not row_id or not title:
        _notice("malformed_row", "跳过一行缺少 id 或 title 的记录")
        return None

    raw_type = _safe_choice(row.get("type"), {"event", "todo"}, default_type)
    if str(row.get("type") or "").strip() and str(row.get("type")).strip() != raw_type:
        _notice("normalised_field", f"type「{row.get('type')}」已归一化为 {raw_type}")

    raw_pri = row.get("priority")
    pri = _safe_choice(raw_pri, _PRIORITIES, "medium")
    # 只在真的有值且值不对时才报，否则每一行空 priority 都会刷一条警告
    if str(raw_pri or "").strip() and str(raw_pri).strip().lower() != pri:
        _notice("normalised_field", f"priority「{raw_pri}」已归一化为 {pri}")

    raw_tp = row.get("time_period")
    tp = _safe_choice(raw_tp, _TIME_PERIODS, None)
    if str(raw_tp or "").strip() and str(raw_tp).strip().lower() != (tp or ""):
        _notice("normalised_field", f"time_period「{raw_tp}」已归一化为 {tp}")

    def txt(field: str) -> Optional[str]:
        v = row.get(field)
        s = "" if v is None else str(v)
        return s or None

    return {
        "id": row_id,
        "type": raw_type,
        "title": title,
        "date": txt("date"),
        "start_time": txt("start_time"),
        "end_time": txt("end_time"),
        "deadline": txt("deadline"),
        "location": txt("location"),
        "time_period": tp,
        "priority": pri,
        "source_text": str(row.get("source_text") or ""),
        "confidence": _safe_float(row.get("confidence")),
        "needs_confirmation": _safe_bool(row.get("needs_confirmation")),
        "is_completed": _safe_bool(row.get("is_completed")),
        "deleted": _safe_bool(row.get("deleted")),
        "created_at": txt("created_at"),
        "updated_at": txt("updated_at"),
    }


# ── 公开 API ────────────────────────────────────────────────────────────────

def _load(kind: str, user: str) -> List[ScheduleItem]:
    global _last_loaded_user
    resp = _request(f"/api/list?type={kind}&user={urllib.parse.quote(str(user or ''))}")
    _absorb_notices(resp)
    if not resp.get("ok"):
        raise RuntimeError(f"读取{kind}失败: {resp.get('error') or resp.get('detail')}")

    # 兜底用的身份。正常写操作走 _acting_user() 读 session_state，不依赖这里。
    if str(user or "").strip():
        _last_loaded_user = str(user).strip()

    items: List[ScheduleItem] = []
    orphans = 0
    for row in resp.get("items") or []:
        if not str(row.get("user") or "").strip():
            orphans += 1
        obj = _row_to_item(row, default_type=kind)
        if obj is None:
            continue
        try:
            items.append(ScheduleItem(**obj))
        except Exception as e:
            _notice("invalid_item", f"跳过一条校验失败的记录 (id={obj['id'][:8]}): {e}")
    if orphans:
        # 与 sheets_storage 一致：无归属行对每个账号都可见，值得提醒运维补 user
        _notice("unattributed_row",
                f"有 {orphans} 条{kind}没有归属用户，所有人都能看到但谁都不能改", "info")
    return items


def load_events(user: str) -> List[ScheduleItem]:
    return _load("event", user)


def load_todos(user: str) -> List[ScheduleItem]:
    return _load("todo", user)


def _save(kind: str, item: ScheduleItem, user: str) -> str:
    sheet = "events" if kind == "event" else "todos"
    payload = {
        "type": kind,
        "user": str(user or ""),
        "item": {
            "id": item.id,
            "type": item.type,
            "title": item.title,
            "date": item.date,
            "start_time": item.start_time,
            "end_time": item.end_time,
            "deadline": item.deadline,
            "location": item.location,
            "time_period": item.time_period,
            "priority": item.priority,
            "source_text": item.source_text,
            "confidence": item.confidence,
            "needs_confirmation": item.needs_confirmation,
            "is_completed": item.is_completed,
            "created_at": item.created_at,
        },
    }
    try:
        resp = _request("/api/save", method="POST", payload=payload)
    except Exception as e:
        # 传输层就失败了——我们无从得知这一条到底写没写进去。
        # 和 sheets_storage 一样先把 outcome 记成 error，别让上一条的
        # saved 状态残留到这一条上。
        _record_save_outcome("error", sheet, item, "", str(e))
        raise

    _absorb_notices(resp)

    status = str(resp.get("status") or "error")
    range_ = str(resp.get("range") or "")
    detail = str(resp.get("detail") or "")

    if status == "skipped":
        _notice("duplicate_id", f"记录已存在，未重复写入 (id={item.id[:8]}…)")
    if status == "error":
        # 传输层成功但 Worker 明确报告写入失败 —— 这时确实没写进去。
        _record_save_outcome("error", sheet, item, range_, detail)
        raise RuntimeError(detail or f"保存失败 (id={item.id[:8]}…)")

    # 走到这里说明已经落库。下面只做记账，任何异常都不得影响返回值。
    try:
        _record_save_outcome(status, sheet, item, range_, detail)
    except Exception as e:  # pragma: no cover - 防御性
        logger.warning("[d1] 记账失败但数据已写入 (id=%s): %s", item.id[:8], e)
    return range_ or f"d1!items/{item.id}"


def save_event(item: ScheduleItem, user: str) -> str:
    return _save("event", item, user)


def save_todo(item: ScheduleItem, user: str) -> str:
    return _save("todo", item, user)


def _delete(kind: str, item_id: str) -> None:
    resp = _request("/api/delete", method="POST",
                    payload={"id": item_id, "user": _acting_user(), "type": kind})
    seen = _absorb_notices(resp)
    if resp.get("ok"):
        return
    # Worker 用 code 区分"没找到"和"没权限"，别把这两种混为一谈：
    # 前者是数据没了，后者是权限问题，用户该知道差在哪。
    # 而且 Worker 已经报过的就别再记一次，否则页面上会出现（×2）。
    code = str(resp.get("code") or "")
    if code == "not_found" and "delete_not_found" not in seen:
        noun = "待办" if kind == "todo" else "日程"
        _notice("delete_not_found",
                f"未找到要删除的{noun} (id={item_id[:8]}…)，可能已被删除")
    elif code == "forbidden" and not seen:
        noun = "待办" if kind == "todo" else "日程"
        _notice("todo_delete_blocked" if kind == "todo" else "delete_blocked",
                f"无权删除该{noun} (id={item_id[:8]}…)")
    elif code not in ("not_found", "forbidden") and not seen:
        noun = "待办" if kind == "todo" else "日程"
        _notice("todo_delete_failed" if kind == "todo" else "delete_failed",
                f"删除{noun}失败 (id={item_id[:8]}…): {resp.get('detail') or '未知原因'}")


def delete_event(item_id: str) -> None:
    _delete("event", item_id)


def delete_todo(item_id: str) -> None:
    _delete("todo", item_id)


def toggle_todo(item_id: str) -> Optional[bool]:
    resp = _request("/api/toggle", method="POST",
                    payload={"id": item_id, "user": _acting_user(), "type": "todo"})
    seen = _absorb_notices(resp)
    if resp.get("ok"):
        return bool(resp.get("is_completed"))
    code = str(resp.get("code") or "")
    if code == "not_found" and "toggle_not_found" not in seen:
        _notice("toggle_not_found",
                f"未找到待办 (id={item_id[:8]}…)，可能已被删除")
    elif code == "forbidden" and not seen:
        # code 里带 todo，app.py 才会把它路由到待办那一栏
        _notice("todo_toggle_blocked", f"无权修改该待办 (id={item_id[:8]}…)")
    elif code not in ("not_found", "forbidden") and not seen:
        _notice("todo_toggle_failed",
                f"勾选失败 (id={item_id[:8]}…): {resp.get('detail') or '未知原因'}")
    return None


def _update(kind: str, item_id: str, updates: Dict) -> bool:
    resp = _request("/api/update", method="POST",
                    payload={"id": item_id, "user": _acting_user(), "updates": updates or {}})
    _absorb_notices(resp)
    if resp.get("ok"):
        return True
    return False


def update_event(item_id: str, updates: Dict) -> bool:
    return _update("event", item_id, updates)


def update_todo(item_id: str, updates: Dict) -> bool:
    return _update("todo", item_id, updates)
