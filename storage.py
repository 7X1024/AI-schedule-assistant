from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from models import ScheduleItem

DATA_DIR = Path(__file__).parent / "data"
EVENTS_FILE = DATA_DIR / "events.json"
TODOS_FILE = DATA_DIR / "todos.json"


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _ensure_file(filepath: Path) -> None:
    _ensure_data_dir()
    if not filepath.exists():
        filepath.write_text("[]", encoding="utf-8")


def _read_items(filepath: Path) -> List[Dict]:
    _ensure_file(filepath)
    raw = filepath.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _write_items(filepath: Path, items: List[Dict]) -> None:
    _ensure_data_dir()
    filepath.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def load_events() -> List[ScheduleItem]:
    raw_items = _read_items(EVENTS_FILE)
    events: List[ScheduleItem] = []
    for obj in raw_items:
        try:
            events.append(ScheduleItem(**obj))
        except Exception as e:
            print(f"[storage] 跳过无效 events 条目: {e}")
    return events


def load_todos() -> List[ScheduleItem]:
    raw_items = _read_items(TODOS_FILE)
    todos: List[ScheduleItem] = []
    for obj in raw_items:
        try:
            todos.append(ScheduleItem(**obj))
        except Exception as e:
            print(f"[storage] 跳过无效 todos 条目: {e}")
    return todos


def save_event(item: ScheduleItem) -> None:
    events = _read_items(EVENTS_FILE)
    events.append(item.model_dump())
    _write_items(EVENTS_FILE, events)


def save_todo(item: ScheduleItem) -> None:
    todos = _read_items(TODOS_FILE)
    todos.append(item.model_dump())
    _write_items(TODOS_FILE, todos)


def delete_event(item_id: str) -> None:
    events = _read_items(EVENTS_FILE)
    events = [e for e in events if e.get("id") != item_id]
    _write_items(EVENTS_FILE, events)


def delete_todo(item_id: str) -> None:
    todos = _read_items(TODOS_FILE)
    todos = [t for t in todos if t.get("id") != item_id]
    _write_items(TODOS_FILE, todos)


def toggle_todo(item_id: str) -> Optional[bool]:
    """Toggle completion. Returns new state or None if not found."""
    todos = _read_items(TODOS_FILE)
    for t in todos:
        if t.get("id") == item_id:
            t["is_completed"] = not t.get("is_completed", False)
            _write_items(TODOS_FILE, todos)
            return t["is_completed"]
    return None


def update_event(item_id: str, updates: Dict) -> bool:
    events = _read_items(EVENTS_FILE)
    for e in events:
        if e.get("id") == item_id:
            e.update(updates)
            _write_items(EVENTS_FILE, events)
            return True
    return False


def update_todo(item_id: str, updates: Dict) -> bool:
    todos = _read_items(TODOS_FILE)
    for t in todos:
        if t.get("id") == item_id:
            t.update(updates)
            _write_items(TODOS_FILE, todos)
            return True
    return False
