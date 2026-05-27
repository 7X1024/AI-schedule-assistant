from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ScheduleItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: Literal["event", "todo"]
    title: str
    date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    deadline: Optional[str] = None
    location: Optional[str] = None
    time_period: Optional[Literal["morning", "noon", "afternoon", "evening", "night"]] = None
    priority: Literal["low", "medium", "high"] = "medium"
    source_text: str
    confidence: float = 0.0
    needs_confirmation: bool = True
    is_completed: bool = False
    deleted: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
