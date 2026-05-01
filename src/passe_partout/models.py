from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Cookie(BaseModel):
    name: str
    value: str
    domain: str | None = None
    path: str | None = None
    expires: float | None = None
    http_only: bool | None = Field(default=None, alias="httpOnly")
    secure: bool | None = None
    same_site: str | None = Field(default=None, alias="sameSite")

    model_config = {"populate_by_name": True}


class CreateTabRequest(BaseModel):
    url: str
    cookies: list[Cookie] | None = None
    ttl_seconds: int | None = None


class TabSummary(BaseModel):
    id: int
    url: str
    created_at: float
    last_used_at: float


class TabState(BaseModel):
    url: str
    title: str
    ready_state: str


class DownloadInfo(BaseModel):
    id: str
    filename: str
    size_bytes: int  # -1 when unknown


class DownloadStatus(BaseModel):
    id: str
    url: str
    filename: str
    state: str  # "in_progress" | "completed" | "canceled"
    bytes_received: int
    size_bytes: int  # -1 when unknown
    started_at: float
    completed_at: float | None


class CreateTabResponse(BaseModel):
    id: int
    status: int
    final_url: str
    content_type: str | None = None
    download: DownloadInfo | None = None


class FetchRequest(BaseModel):
    url: str
    cookies: list[Cookie] | None = None
    ttl_seconds: int | None = None


class FetchResponse(BaseModel):
    status: int
    final_url: str
    html: str
    content_type: str | None = None


class GotoRequest(BaseModel):
    url: str


class GotoResponse(BaseModel):
    status: int
    final_url: str
    content_type: str | None = None
    download: DownloadInfo | None = None


class ClickRequest(BaseModel):
    selector: str


class TypeRequest(BaseModel):
    selector: str
    text: str


class EvalRequest(BaseModel):
    js: str


class EvalResponse(BaseModel):
    result: Any


class WaitRequest(BaseModel):
    selector: str | None = None
    network_idle: bool | None = None
    timeout_ms: int | None = None


class HealthResponse(BaseModel):
    ok: bool
    browser: str
    tabs: int


class ErrorBody(BaseModel):
    error: str
    detail: str
