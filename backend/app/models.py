from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IncidentCreate(BaseModel):
    title: str = Field(..., min_length=3)
    summary: str = Field(..., min_length=5)
    severity: str = Field(default="High")
    services: str = Field(default="")
    source: str = Field(default="manual")
    logs: str = Field(default="")


class IncidentOutcomeUpdate(BaseModel):
    status: str = Field(default="resolved")
    outcome: str = Field(default="")
    success: bool = True


class IncidentActionCreate(BaseModel):
    description: str = Field(..., min_length=10, max_length=2000)
    rationale: str = Field(default="", max_length=2000)


class IncidentActionDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=1000)


class IncidentActionExecution(BaseModel):
    execution_note: str = Field(..., min_length=3, max_length=1000)


class InvestigationRequest(BaseModel):
    logs: str = Field(default="")
    title: str | None = None
    services: str | None = None

