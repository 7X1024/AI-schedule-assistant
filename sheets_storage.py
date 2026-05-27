from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import streamlit as st
from google.oauth2 import service_account
from google.oauth2.service_account import Credentials

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
]

TODOS_HEADERS = EVENTS_HEADERS  # identical schema


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

    credentials: Credentials = service_account.Credentials.from_service_account_info(
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
    """Get or create worksheet. Only writes headers if sheet is empty.
    Never clears or overwrites existing data."""
    spreadsheet = _get_spreadsheet()
    try:
        ws = spreadsheet.worksheet(name)
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=100, cols=16)
        ws.append_row(headers)
        return ws

    # worksheet exists – only write headers if sheet is genuinely empty
    values = ws.get_all_values()
    if len(values) == 0:
        ws.append_row(headers)

    return ws


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


def _row_to_item(row: List[str]) -> Optional[dict]:
    """Convert a Google Sheets row (list of strings) to a dict for Pydantic.
    Returns None if the row should be skipped (deleted or invalid)."""
    while len(row) < len(EVENTS_HEADERS):
        row.append("")

    deleted = _safe_bool(row[13])
    if deleted:
        return None

    return {
        "id": row[0],
        "type": row[1],
        "title": row[2],
        "date": row[3] or None,
        "start_time": row[4] or None,
        "end_time": row[5] or None,
        "deadline": row[6] or None,
        "location": row[7] or None,
        "time_period": None,
        "priority": row[8] or "medium",
        "source_text": row[9],
        "confidence": _safe_float(row[10]),
        "needs_confirmation": _safe_bool(row[11]),
        "is_completed": _safe_bool(row[12]),
        "deleted": deleted,
        "created_at": row[14] or None,
        "updated_at": row[15] or None,
    }


def _item_to_row(item: ScheduleItem) -> List[str]:
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
    ]


# ── public API (mirrors storage.py) ──────────────────────────────────────────

def load_events() -> List[ScheduleItem]:
    ws = _ensure_worksheet("events", EVENTS_HEADERS)
    values = ws.get_all_values()
    if len(values) <= 1:
        return []
    items: List[ScheduleItem] = []
    for row in values[1:]:
        if not row or not row[2].strip():
            continue
        obj = _row_to_item(row)
        if obj is None:
            continue
        try:
            items.append(ScheduleItem(**obj))
        except Exception as e:
            st.warning(f"⚠️ 跳过无效 events 条目 (id={obj.get('id', '?')[:8]}): {e} → 原始行: {row[:4]}")
    return items


def load_todos() -> List[ScheduleItem]:
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    values = ws.get_all_values()
    if len(values) <= 1:
        return []
    items: List[ScheduleItem] = []
    for row in values[1:]:
        if not row or not row[2].strip():
            continue
        obj = _row_to_item(row)
        if obj is None:
            continue
        try:
            items.append(ScheduleItem(**obj))
        except Exception as e:
            st.warning(f"⚠️ 跳过无效 todos 条目 (id={obj.get('id', '?')[:8]}): {e} → 原始行: {row[:4]}")
    return items


def save_event(item: ScheduleItem) -> None:
    ws = _ensure_worksheet("events", EVENTS_HEADERS)
    row = _item_to_row(item)
    ws.append_row(row)


def save_todo(item: ScheduleItem) -> None:
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    row = _item_to_row(item)
    ws.append_row(row)


def delete_event(item_id: str) -> None:
    """Soft delete: sets deleted=TRUE, updates updated_at."""
    ws = _ensure_worksheet("events", EVENTS_HEADERS)
    values = ws.get_all_values()
    for i, row in enumerate(values):
        if len(row) > 0 and row[0] == item_id:
            row_idx = i + 1  # 1-based
            ws.update_cell(row_idx, 14, "TRUE")       # deleted (col 14)
            ws.update_cell(row_idx, 16, datetime.now().isoformat())  # updated_at
            return


def delete_todo(item_id: str) -> None:
    """Soft delete: sets deleted=TRUE, updates updated_at."""
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    values = ws.get_all_values()
    for i, row in enumerate(values):
        if len(row) > 0 and row[0] == item_id:
            row_idx = i + 1
            ws.update_cell(row_idx, 14, "TRUE")
            ws.update_cell(row_idx, 16, datetime.now().isoformat())
            return


def toggle_todo(item_id: str) -> Optional[bool]:
    """Toggle completion. Returns new state or None if not found."""
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    values = ws.get_all_values()
    for i, row in enumerate(values):
        if len(row) > 0 and row[0] == item_id:
            row_idx = i + 1
            current = row[12].strip().lower() in ("true", "1", "yes") if len(row) > 12 else False
            new_val = "FALSE" if current else "TRUE"
            ws.update_cell(row_idx, 13, new_val)  # completed (col 13)
            ws.update_cell(row_idx, 16, datetime.now().isoformat())  # updated_at
            return not current
    return None


def update_event(item_id: str, updates: Dict) -> bool:
    """Update specific fields of an event row by id. Returns True if found."""
    col_map = {h: idx + 1 for idx, h in enumerate(EVENTS_HEADERS)}
    ws = _ensure_worksheet("events", EVENTS_HEADERS)
    values = ws.get_all_values()
    for i, row in enumerate(values):
        if len(row) > 0 and row[0] == item_id:
            row_idx = i + 1
            for field, value in updates.items():
                if field in col_map:
                    ws.update_cell(row_idx, col_map[field], str(value))
            ws.update_cell(row_idx, 16, datetime.now().isoformat())
            return True
    return False


def update_todo(item_id: str, updates: Dict) -> bool:
    """Update specific fields of a todo row by id. Returns True if found."""
    col_map = {h: idx + 1 for idx, h in enumerate(TODOS_HEADERS)}
    ws = _ensure_worksheet("todos", TODOS_HEADERS)
    values = ws.get_all_values()
    for i, row in enumerate(values):
        if len(row) > 0 and row[0] == item_id:
            row_idx = i + 1
            for field, value in updates.items():
                if field in col_map:
                    ws.update_cell(row_idx, col_map[field], str(value))
            ws.update_cell(row_idx, 16, datetime.now().isoformat())
            return True
    return False
