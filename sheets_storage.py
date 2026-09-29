from __future__ import annotations

import logging
import re
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
from google.oauth2 import service_account

import gspread
from gspread.exceptions import WorksheetNotFound

from models import ScheduleItem

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

TODOS_HEADERS = EVENTS_HEADERS  # identical schema

_USER_COL = EVENTS_HEADERS.index("user")  # 17, 0-based

# R5 — read *and* write policy for rows whose `user` cell is blank (the sheet
# predates the column, or the cell was hand-edited).
#   read : LEGACY_USER_OWNER=None (default) → shown to every account, so
#          nobody's data silently disappears. Set to a name to restrict them
#          to that single account instead.
#   write: nobody may modify an unattributable row unless LEGACY_USER_OWNER is
#          set to a name — i.e. the default is "no account, not even the one
#          that can see it, may edit or delete it". A blank-user row is visible
#          to all and writable by none; filling in the `user` column restores
#          full per-account read/write isolation.
LEGACY_USER_OWNER: Optional[str] = None

# F5 — read-after-write verification is retried, never fatal. R7: a failed
# read-back must not turn into a blocked UI thread, so this is one short retry
# (≈0.2 s worst case) instead of three blocking ones (≈0.8 s per item).
VERIFY_ATTEMPTS = 2
VERIFY_DELAY_S = 0.2

# R4 — save_event/save_todo may only return `str`, so a *skipped* save has to
# be distinguishable from a written one inside the string itself. Any return
# value containing this marker means "nothing was written to the sheet".
SKIPPED_MARKER = "[skipped:duplicate-id]"

logger = logging.getLogger(__name__)


# ── diagnostics (F6) ─────────────────────────────────────────────────────────
# st.warning() used to be called from inside this module, so one bad row or one
# stale delete painted a banner above the whole app on every rerun, forever.
# The public signatures are fixed, so conditions are reported here instead:
# written to the app log and kept in a bounded, de-duplicated buffer that
# app.py can render in context via get_diagnostics() — or ignore safely.
NOTICE_LIMIT = 50
_notices: "deque[Dict[str, Any]]" = deque(maxlen=NOTICE_LIMIT)
_notice_counts: Dict[str, int] = {}


def _notice(code: str, message: str, level: str = "warning") -> None:
    """Record a condition without touching the page (see F6).

    Log lines are emitted for the first few occurrences of each code only, so a
    condition that survives on every rerun cannot flood the terminal; the
    running count keeps it observable.
    """
    count = _notice_counts.get(code, 0) + 1
    _notice_counts[code] = count
    if count <= 3:
        getattr(logger, level, logger.warning)("[sheets] %s: %s", code, message)
    for notice in reversed(_notices):
        if notice["code"] == code:
            notice["count"] = count
            notice["message"] = message
            break
    else:
        _notices.append({
            "code": code,
            "level": level,
            "count": count,
            "message": message,
            "first_seen": datetime.now().isoformat(timespec="seconds"),
        })


def get_diagnostics() -> Dict[str, Dict[str, Any]]:
    """Non-intrusive condition report for app.py to render (or ignore).

    Public shape (R6) — a plain JSON-serialisable ``dict`` keyed by notice
    code, so a caller can iterate it, look a code up directly, or hand it to
    ``st.json`` unchanged::

        {
          "<code>": {
            "code":     str,   # same string as the key
            "level":    str,   # "warning" | "info" | "error"
            "count":    int,   # occurrences since the last clear_diagnostics()
            "message":  str,   # most recent human-readable description
            "first_seen": str  # ISO timestamp of the first occurrence
          },
          ...
        }

    Cheap and side-effect free; safe to call on every rerun. The returned
    object is a fresh copy — mutating it does not affect the buffer.
    """
    return {n["code"]: dict(n) for n in _notices}


def clear_diagnostics() -> None:
    """Drop every recorded condition and its counters."""
    _notices.clear()
    _notice_counts.clear()


# ── last-save outcome (R4) ───────────────────────────────────────────────────
# save_event/save_todo must keep returning `str`, but a *skipped* save has to
# be distinguishable from a written one. The returned string carries
# SKIPPED_MARKER for that; this record carries the machine-readable form.
_last_save_outcome: Dict[str, Any] = {
    "status": "none",       # "none" | "saved" | "skipped" | "error"
    "saved": False,         # True only when a row was actually appended
    "skipped": False,       # True when the write was deliberately not done
    "sheet": "",            # "events" | "todos"
    "item_id": "",
    "range": "",            # destination range returned to the caller
    "detail": "",           # human-readable reason
}


def get_last_save_outcome() -> Dict[str, Any]:
    """Outcome of the most recent save_event()/save_todo() call.

    R4 — app.py counts `save_*` return values as successes, so a skipped save
    used to be reported to the user as saved. This is the machine-readable
    form of that verdict; the returned string carries SKIPPED_MARKER for the
    same case. Returns a copy; the initial value is
    ``{"status": "none", "saved": False, "skipped": False, ...}``.

    Shape (fixed keys, JSON-serialisable)::

        {
          "status":  "none" | "saved" | "skipped" | "error",
          "saved":   bool,  # True iff a row was actually appended
          "skipped": bool,  # True iff the write was deliberately not done
          "sheet":   str,   # "events" | "todos" ("" before the first save)
          "item_id": str,   # id of the item that was just submitted
          "range":   str,   # destination A1 range returned to the caller
          "detail":  str,   # human-readable reason
        }

    When it is written — exactly once per save call, before it returns:

    * "skipped" — this id already had a live (non-deleted) row; nothing was
      appended and the returned string contains SKIPPED_MARKER.
    * "error"   — append_row itself failed and the exception propagated, so
      no row was written. No read-back outcome ever produces "error".
    * "saved"   — the row was appended. A read-back that misses, or that
      cannot run at all, only changes ``detail``; it can never turn an
      appended row into ``saved: False`` (R1).
    * "none"    — the initial value, before any save has been attempted.

    **Read it immediately after every save_event/save_todo call.** It is one
    module slot holding the *most recent* save, not a per-item log: a batch
    loop that saves five items and reads once at the end sees item #5 only::

        for it in items:
            save_event(it, user)
            results.append(get_last_save_outcome())   # inside the loop
    """
    return dict(_last_save_outcome)


def _record_save_outcome(status: str, sheet_name: str, item: ScheduleItem,
                         range_: str = "", detail: str = "") -> Dict[str, Any]:
    _last_save_outcome.update({
        "status": status,
        "saved": status == "saved",
        "skipped": status == "skipped",
        "sheet": sheet_name,
        "item_id": item.id,
        "range": range_,
        "detail": detail,
    })
    # A row is already written by the time we get here. The outcome accessor is
    # bookkeeping only, so it must never be able to turn a completed append into
    # a raised error — that would report "保存失败" for data that is in the sheet.
    try:
        return get_last_save_outcome()
    except Exception:  # pragma: no cover - defensive
        return dict(_last_save_outcome)


# ── Google Sheets client (lazy init) ─────────────────────────────────────────
_gs_client: Optional[gspread.Client] = None
_gs_spreadsheet = None


def _get_client() -> gspread.Client:
    global _gs_client
    if _gs_client is not None:
        return _gs_client

    raw = dict(st.secrets["gcp"])
    private_key = raw.get("private_key", "")
    if "\\n" in private_key:
        raw["private_key"] = private_key.replace("\\n", "\n")

    credentials: service_account.Credentials = service_account.Credentials.from_service_account_info(
        raw,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    _gs_client = gspread.authorize(credentials)
    return _gs_client


def _get_spreadsheet():
    global _gs_spreadsheet
    if _gs_spreadsheet is not None:
        return _gs_spreadsheet
    client = _get_client()
    sheet_id = st.secrets["GOOGLE_SHEETS_ID"]
    _gs_spreadsheet = client.open_by_key(sheet_id)
    return _gs_spreadsheet


# ── worksheet helpers ────────────────────────────────────────────────────────
def _ensure_worksheet(name: str, headers: List[str]):
    """Get or create worksheet. Never clears or overwrites existing data.

    R1 — the worksheet handle is resolved on *every* call and never cached.
    A cached handle is a permanent outage: once the tab is deleted and
    recreated (routine in Sheets) the old sheetId 404s forever, and a
    long-lived Streamlit Cloud process has no path that would ever drop it.
    One metadata GET per call is the price of being self-healing.
    """
    spreadsheet = _get_spreadsheet()
    try:
        ws = spreadsheet.worksheet(name)
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=1, cols=18)
        ws.update("A1", [headers])
    return ws


def _cell_value(ws, row: int, col: int, default: str = "") -> str:
    """Read a single cell (one small GET on the real API).

    Falls back to a column fetch if the client has no cell accessor, so this
    never raises on a read path that must not kill the app.
    """
    try:
        cell = ws.cell(row, col)
    except Exception:
        try:
            values = ws.col_values(col)
        except Exception:  # narrow grid / no permission: treat as empty
            return default
        return values[row - 1] if 0 < row <= len(values) else default
    value = getattr(cell, "value", default)
    return default if value is None else str(value)


def _row_index_in(
    ids: List[str],
    item_id: str,
    deleted: Optional[List[bool]] = None,
) -> Optional[int]:
    """1-based sheet row of the first *live* row whose id equals `item_id`.

    R3/R4 — both sides are stripped: a hand-typed or machine-written id with a
    stray space must still match, otherwise the dedup silently fails and the
    same item gets appended twice. When `deleted` is supplied (column 14), a
    soft-deleted row is not a match at all: `load_*` filters those out, so
    treating one as a duplicate would drop the re-saved item into a hole it
    can never be read back from, and writing to one would edit a record the
    caller can no longer see.
    """
    wanted = str(item_id or "").strip()
    if not wanted:
        return None
    for idx, value in enumerate(ids, start=1):
        if str(value or "").strip() != wanted:
            continue
        if deleted is not None and 0 < idx <= len(deleted) and deleted[idx - 1]:
            continue
        return idx
    return None


def _find_row_by_id(ws, item_id: str) -> Optional[int]:
    """1-based sheet row number of a live row by id, or None.

    Reads the id column, and only pays for the extra `deleted` column read when
    an id actually collides. Row indexes line up with the get_all_values()
    enumeration because both start at sheet row 1, so `row_idx` stays valid
    for update_cell(). A soft-deleted row is never returned: it is invisible
    to the caller, so an id that now only exists on such a row is reported as
    not found rather than silently edited.
    """
    ids = ws.col_values(1)
    found = _row_index_in(ids, item_id)
    if found is None:
        return None
    deleted = [_safe_bool(v) for v in ws.col_values(14)]
    if found <= len(deleted) and deleted[found - 1]:
        return _row_index_in(ids, item_id, deleted)
    return found


def _safe_float(val: str) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _safe_bool(val: str) -> bool:
    try:
        return val.strip().lower() in ("true", "1", "yes")
    except (AttributeError, TypeError):
        return False


def _safe_choice(value: str, choices, default: Optional[str] = None) -> Optional[str]:
    """Normalise a cell to one of `choices`, or fall back to `default`.

    R3 — Pydantic `Literal` rejects `' high '`, so one stray space used to fail
    validation, get swallowed by the load loop, and make the row disappear from
    its owner's board with nothing but a log line. Coerce instead of dropping.
    """
    text = str(value or "").strip().lower()
    return text if text in choices else default


_TYPE_CHOICES = ("event", "todo")
_PRIORITY_CHOICES = ("low", "medium", "high")
_PERIOD_CHOICES = ("morning", "noon", "afternoon", "evening", "night")


def _row_to_item(row: List[str], default_type: str = "event") -> Optional[dict]:
    """Convert a Google Sheets row (list of strings) to a dict for Pydantic.
    Returns None if the row should be skipped (deleted or invalid).

    R3 — `type`/`priority`/`time_period` are normalised and `id` is stripped,
    so sloppy cell content can no longer make a row vanish.
    """
    while len(row) < len(EVENTS_HEADERS):
        row.append("")

    deleted = _safe_bool(row[13])
    if deleted:
        return None

    raw_type, raw_priority, raw_period = row[1], row[8], row[16]
    kind = _safe_choice(raw_type, _TYPE_CHOICES)
    if kind is None:
        kind = _safe_choice(default_type, _TYPE_CHOICES, "event")
        _notice("normalised_field", f"type={raw_type!r} 无法识别，按 {kind!r} 处理（应为 event/todo）")
    priority = _safe_choice(raw_priority, _PRIORITY_CHOICES)
    if priority is None:
        priority = "medium"
        if str(raw_priority or "").strip():
            _notice("normalised_field", f"priority={raw_priority!r} 无法识别，按 'medium' 处理")
    period = _safe_choice(raw_period, _PERIOD_CHOICES)
    if period is None:
        period = None
        if str(raw_period or "").strip():
            _notice("normalised_field", f"time_period={raw_period!r} 无法识别，按空处理")

    return {
        "id": row[0].strip(),
        "type": kind,
        "title": row[2],
        "date": row[3] or None,
        "start_time": row[4] or None,
        "end_time": row[5] or None,
        "deadline": row[6] or None,
        "location": row[7] or None,
        "time_period": period,
        "priority": priority,
        "source_text": row[9],
        "confidence": _safe_float(row[10]),
        "needs_confirmation": _safe_bool(row[11]),
        "is_completed": _safe_bool(row[12]),
        "deleted": deleted,
        "created_at": row[14] or None,
        "updated_at": row[15] or None,
    }


def _item_to_row(item: ScheduleItem, user: str) -> List[str]:
    """Convert a ScheduleItem to a row list for Google Sheets append."""
    now = datetime.now().isoformat()
    return [
        item.id,
        item.type,
        item.title,
        item.date or "",
        item.start_time or "",
        item.end_time or "",
        item.deadline or "",
        item.location or "",
        item.priority,
        item.source_text,
        str(item.confidence),
        str(item.needs_confirmation),
        str(item.is_completed),
        str(item.deleted),
        item.created_at or now,
        item.updated_at or now,
        item.time_period or "",
        user or "",
    ]


def _worksheet_has_item(ws, item_id: str) -> bool:
    """Confirm a row is visible in the sheet by checking the id column."""
    return _find_row_by_id(ws, item_id) is not None


def _updated_range(response, sheet_name: str) -> str:
    """Human-readable destination of an append, for the success message."""
    if isinstance(response, dict):
        updates = response.get("updates", {})
        updated_range = updates.get("updatedRange") if isinstance(updates, dict) else None
        if updated_range:
            return str(updated_range)
    return f"{sheet_name} 表页"


def _row_from_range(ref: str) -> Optional[int]:
    """Parse the 1-based row number out of an A1 range like "todos!A12:R12"."""
    match = re.search(r"[A-Z]+(\d+)", str(ref).upper())
    return int(match.group(1)) if match else None


def _verify_written(ws, item_id: str, updated_range: str) -> Optional[bool]:
    """Read-after-write check (F5). Returns:

    * ``True``  — the row is visible in the sheet;
    * ``False`` — the read completed and the row was genuinely not there;
    * ``None``  — the read could not be completed at all (API trouble, a
      client with no cell/column accessor, …).

    R7 — narrows the read to the single id cell of the row the API said it
    wrote, and retries once (VERIFY_DELAY_S, module-level so it stays
    tunable) so propagation lag does not turn a successful write into a
    failure.

    R2 — `item_id` is normalised exactly the way `_row_index_in` normalises
    it on the dedup path, so a whitespace-padded id cannot make this path
    and the dedup path disagree about whether the row exists.

    R1 — never raises, *including* on the column-read fallback that used to
    sit outside the `try`: by this point the row has already been appended,
    so a failing read may only downgrade the answer to ``None``, never abort
    the save. A "verification" that raises or stalls the batch is itself a
    failure amplifier — that is also why the three 0.4 s blocking sleeps were
    cut to one 0.2 s retry (≈0.81 s → ≈0.2 s per item).
    """
    wanted = str(item_id or "").strip()
    target_row = _row_from_range(updated_range)
    last_read_failed = False
    for attempt in range(VERIFY_ATTEMPTS):
        try:
            if target_row is not None:
                if _cell_value(ws, target_row, 1).strip() == wanted:
                    return True
            elif _worksheet_has_item(ws, wanted):
                return True
            last_read_failed = False
        except Exception as exc:  # client cannot read the sheet at all
            logger.debug("[sheets] read-back unavailable, falling back: %s", exc)
            target_row = None
            last_read_failed = True
        if attempt + 1 < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_DELAY_S)
    return None if last_read_failed else False


def _sheet_is_empty(ws) -> bool:
    """True when *no* cell in the sheet has content (AGENTS.md storage rule 5).

    R2 — the earlier check looked at column A only. A sheet holding real data
    in B..R with a blank column A looked "empty" and the header write
    overwrote row 1, destroying the first data row. Only a whole-sheet read
    can answer this safely.
    """
    return not any(
        str(cell).strip() for row in ws.get_all_values() for cell in row
    )


def _append_item(sheet_name: str, headers: List[str], item: ScheduleItem, user: str) -> str:
    ws = _ensure_worksheet(sheet_name, headers)
    row = _item_to_row(item, user)

    # One narrow fetch of the id column, reused for the header check, the
    # duplicate check and the read-back.
    ids = ws.col_values(1)
    if not any(str(value or "").strip() for value in ids) and _sheet_is_empty(ws):
        # Sheet exists but holds nothing at all: write the header row before
        # the first append so the column layout is never shifted. The full
        # read above is only paid on this path — a sheet that already has
        # anything in column A never gets here.
        ws.update("A1", [headers])
        ids = ws.col_values(1)

    # Idempotent on item.id. A retry after a partially failed batch must not
    # append a second row with the same id: two rows sharing an id make
    # load_events return two items, which collides on app.py's form keys and
    # makes the whole app unopenable. An existing row is left untouched (this
    # log is append-only; edits go through update_event/update_todo).
    #
    # R4 — a *soft-deleted* row does not count as a duplicate: load_* filters
    # those out, so skipping here would drop the re-saved item into a hole it
    # can never be read back from.
    existing = _row_index_in(ids, item.id)
    if existing is not None:
        # Only a collision costs the extra `deleted` column read.
        existing = _row_index_in(ids, item.id, [_safe_bool(v) for v in ws.col_values(14)])
    if existing is not None:
        detail = f"第 {existing} 行已存在同一 id 的未删除记录，未重复追加"
        _notice("duplicate_id", f"{sheet_name} {detail}（id={item.id[:8]}…）")
        _record_save_outcome("skipped", sheet_name, item,
                             f"{sheet_name}!A{existing}", detail)
        return f"{sheet_name}!A{existing}（id 已存在，未重复追加）{SKIPPED_MARKER}"

    if not user:
        _notice("blank_user", f"保存 {sheet_name} 条目时 user 为空，该行将无法归属到任何账号，"
                               f"且在补上 user 列之前任何人都无法修改或删除它")

    try:
        response = ws.append_row(row, value_input_option="RAW")
    except Exception as exc:
        # R4 — never leave a stale "saved" record behind: the caller may go on
        # to read get_last_save_outcome() after catching this.
        _record_save_outcome("error", sheet_name, item, "", f"{type(exc).__name__}: {exc}")
        raise
    destination = _updated_range(response, sheet_name)

    # R1 — the row is in the sheet from here on, whatever the read-back says.
    # Verification can only shape `detail`, never the verdict and never an
    # exception: `saved` is the truth because append_row returned.
    try:
        verified = _verify_written(ws, item.id, destination)
    except Exception as exc:  # belt and braces: _verify_written is non-raising
        logger.debug("[sheets] read-back raised unexpectedly: %s", exc)
        verified = None
    if verified is False:
        _notice(
            "readback_miss",
            f"Google Sheets 写入返回成功（{destination}），但 {sheet_name} 表页暂时回查不到 "
            f"id={item.id[:8]}…；数据已写入，刷新后应可见",
        )
        detail = "已写入（回查暂未看到该行，刷新后应可见）"
    elif verified is None:
        _notice(
            "readback_unavailable",
            f"Google Sheets 写入返回成功（{destination}），但 {sheet_name} 表页暂时无法回查"
            f"（id={item.id[:8]}…）；数据已写入，可稍后刷新确认",
            level="info",
        )
        detail = "已写入（回查不可用，未能验证；数据已写入）"
    else:
        detail = "已写入"

    _record_save_outcome("saved", sheet_name, item, destination, detail)
    return destination


# ── public API (mirrors storage.py) ──────────────────────────────────────────

def _row_user(row: List[str]) -> str:
    """Owner recorded in the `user` column; "" when blank or the column is
    missing. Never guesses a value (F3)."""
    if len(row) <= _USER_COL:
        return ""
    return row[_USER_COL].strip()


def _matches_user(row_user: str, user: str) -> bool:
    """Should this row be shown to `user`?

    F3 — a blank user cell is unattributable, not "7X". The old `or "7X"`
    fallback meant a sheet predating the `user` column gave *every* account's
    rows to 7X and left Jasper / lanmao / RBumaro with permanently empty
    boards; but dropping such rows would instead hide the data from everyone.
    So unattributable rows are shown to every account (LEGACY_USER_OWNER=None)
    and counted, rather than being silently misattributed or silently lost.
    Rows that *do* carry a user are still strictly filtered.
    """
    if not user:
        return True
    if row_user:
        return row_user == user
    if LEGACY_USER_OWNER is None:
        _notice(
            "unattributed_row",
            f"有条目没有 user 归属，已对所有账号可见（{user}）；填上 user 列即可恢复隔离",
            level="info",
        )
        return True
    return LEGACY_USER_OWNER == user


def load_events(user: str) -> List[ScheduleItem]:
    ws = _ensure_worksheet("events", EVENTS_HEADERS)
    values = ws.get_all_values()
    if len(values) <= 1:
        return []
    items: List[ScheduleItem] = []
    for row in values[1:]:
        # F2 — guard the length before indexing: a stray/short row must be
        # skipped, not raise IndexError and take the whole app down.
        if len(row) < 3 or not row[2].strip():
            if any(cell.strip() for cell in row):
                _notice("malformed_row", f"跳过无效 events 行: {row[:4]}")
            continue
        if not _matches_user(_row_user(row), user):
            continue
        obj = _row_to_item(row, default_type="event")
        if obj is None:
            continue
        try:
            items.append(ScheduleItem(**obj))
        except Exception as e:
            _notice("invalid_item", f"跳过无效 events 条目 (id={obj.get('id', '?')[:8]}): {e} → 原始行: {row[:4]}")
    return items


def load_todos(user: str) -> List[ScheduleItem]:
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    values = ws.get_all_values()
    if len(values) <= 1:
        return []
    items: List[ScheduleItem] = []
    for row in values[1:]:
        if len(row) < 3 or not row[2].strip():
            if any(cell.strip() for cell in row):
                _notice("malformed_row", f"跳过无效 todos 行: {row[:4]}")
            continue
        if not _matches_user(_row_user(row), user):
            continue
        obj = _row_to_item(row, default_type="todo")
        if obj is None:
            continue
        try:
            items.append(ScheduleItem(**obj))
        except Exception as e:
            _notice("invalid_item", f"跳过无效 todos 条目 (id={obj.get('id', '?')[:8]}): {e} → 原始行: {row[:4]}")
    return items


def save_event(item: ScheduleItem, user: str) -> str:
    return _append_item("events", EVENTS_HEADERS, item, user)


def save_todo(item: ScheduleItem, user: str) -> str:
    return _append_item("todos", TODOS_HEADERS, item, user)


def _row_is_writable(ws, row_idx: int, sheet_name: str) -> bool:
    """R5 — write policy for a row whose `user` cell is blank.

    A row that names an owner is writable by whoever reaches it (app.py only
    ever renders rows that `load_*` returned for the signed-in account, so
    the read filter is the access control). A row with *no* owner has no
    basis for that claim, so it is visible to every account but writable by
    none — unless an operator has explicitly designated a custodian via
    LEGACY_USER_OWNER. A refusal is reported, never silent.
    """
    row_user = _cell_value(ws, row_idx, _USER_COL + 1).strip()
    if row_user:
        return True
    if LEGACY_USER_OWNER is not None:
        return True
    _notice(
        "unattributed_write_blocked",
        f"{sheet_name} 第 {row_idx} 行没有 user 归属，已拒绝修改/删除；"
        f"补上 user 列（或设置 LEGACY_USER_OWNER）后由指定账号接管",
    )
    return False


def delete_event(item_id: str) -> None:
    """Soft delete: sets deleted=TRUE, updates updated_at."""
    ws = _ensure_worksheet("events", EVENTS_HEADERS)
    row_idx = _find_row_by_id(ws, item_id)
    if row_idx is not None:
        if not _row_is_writable(ws, row_idx, "events"):
            return
        ws.update_cell(row_idx, 14, "TRUE")  # deleted (col 14)
        ws.update_cell(row_idx, 16, datetime.now().isoformat())  # updated_at
        return
    # F6 — reported, not painted over the page on every rerun.
    _notice("delete_not_found", f"未找到要删除的事件 (id={item_id[:8]}…)，可能已被删除")


def delete_todo(item_id: str) -> None:
    """Soft delete: sets deleted=TRUE, updates updated_at."""
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    row_idx = _find_row_by_id(ws, item_id)
    if row_idx is not None:
        if not _row_is_writable(ws, row_idx, "todos"):
            return
        ws.update_cell(row_idx, 14, "TRUE")
        ws.update_cell(row_idx, 16, datetime.now().isoformat())
        return
    _notice("delete_not_found", f"未找到要删除的待办 (id={item_id[:8]}…)，可能已被删除")


def toggle_todo(item_id: str) -> Optional[bool]:
    """Toggle completion. Returns new state or None if not found."""
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    row_idx = _find_row_by_id(ws, item_id)
    if row_idx is None:
        _notice("toggle_not_found", f"未找到要切换的待办 (id={item_id[:8]}…)，可能已被删除")
        return None
    if not _row_is_writable(ws, row_idx, "todos"):
        return None
    current = _cell_value(ws, row_idx, 13).strip().lower() in ("true", "1", "yes")
    new_val = "FALSE" if current else "TRUE"
    ws.update_cell(row_idx, 13, new_val)  # completed (col 13)
    ws.update_cell(row_idx, 16, datetime.now().isoformat())  # updated_at
    return not current


# F4 — the sheet header is `completed`, the model field is `is_completed`. The
# old col_map was keyed by header name only, so `{"is_completed": True}` was
# dropped silently while the function still returned True.
_FIELD_ALIASES = {"is_completed": "completed"}


def _column_map(headers: List[str]) -> Dict[str, int]:
    """1-based column numbers keyed by BOTH the sheet header names and the
    ScheduleItem field names (F4)."""
    col_map = {header: idx + 1 for idx, header in enumerate(headers)}
    for field, header in _FIELD_ALIASES.items():
        if header in col_map:
            col_map[field] = col_map[header]
    return col_map


def _apply_updates(ws, row_idx: int, headers: List[str], updates: Dict) -> Tuple[int, List[str]]:
    """Write the recognised fields of `updates` onto row `row_idx`.

    Returns (fields_written, unknown_field_names). An unrecognised field name is
    reported through the diagnostics channel instead of vanishing (F4), so this
    class of bug cannot hide again.
    """
    col_map = _column_map(headers)
    unknown = [field for field in updates if field not in col_map]
    if unknown:
        _notice(
            "unknown_field",
            f"{ws.title} 第 {row_idx} 行忽略了无法识别的字段 {sorted(unknown)}；"
            f"可识别字段 = {sorted(col_map)}",
        )
    written = 0
    for field, value in updates.items():
        col = col_map.get(field)
        if col is None:
            continue
        ws.update_cell(row_idx, col, "" if value is None else str(value))
        written += 1
    return written, unknown


def update_event(item_id: str, updates: Dict) -> bool:
    """Update specific fields of an event row by id. Returns True if the row was
    found and the update applied."""
    ws = _ensure_worksheet("events", EVENTS_HEADERS)
    row_idx = _find_row_by_id(ws, item_id)
    if row_idx is None:
        return False
    if not _row_is_writable(ws, row_idx, "events"):  # R5
        return False
    written, _unknown = _apply_updates(ws, row_idx, EVENTS_HEADERS, updates)
    if written == 0 and updates:
        # F4 — found, but nothing was writable: never report a silent success.
        return False
    if written:
        ws.update_cell(row_idx, 16, datetime.now().isoformat())  # updated_at
    return True


def update_todo(item_id: str, updates: Dict) -> bool:
    """Update specific fields of a todo row by id. Returns True if the row was
    found and the update applied."""
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    row_idx = _find_row_by_id(ws, item_id)
    if row_idx is None:
        return False
    if not _row_is_writable(ws, row_idx, "todos"):  # R5
        return False
    written, _unknown = _apply_updates(ws, row_idx, TODOS_HEADERS, updates)
    if written == 0 and updates:
        return False
    if written:
        ws.update_cell(row_idx, 16, datetime.now().isoformat())
    return True
