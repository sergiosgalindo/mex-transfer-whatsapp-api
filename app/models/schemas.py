from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RetrievedSource(BaseModel):
    id: str
    category: str
    question: str
    answer: str
    score: float


class ChatResponse(BaseModel):
    reply: str
    sources: List[RetrievedSource] = Field(default_factory=list)
    model: str
    session_id: Optional[str] = None
    active_use_case: Optional[str] = None
    tool_executed: Optional[str] = None
    tool_output: Optional[Dict[str, Any]] = None
    interactive_menu: Optional[Dict[str, Any]] = None
