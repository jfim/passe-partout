"""Pydantic request/response models for the REST API."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

# Bounds chosen to be generous for legitimate use while preventing accidental DoS
# via multi-megabyte fields. Adjust upward if a real workload hits them.
URL_MAX = 8_192
SELECTOR_MAX = 2_048
JS_MAX = 1_048_576  # 1 MiB
TEXT_MAX = 1_048_576
COOKIE_NAME_MAX = 256
COOKIE_VALUE_MAX = 4_096
COOKIE_DOMAIN_MAX = 253
COOKIE_PATH_MAX = 1_024


def _no_control_chars(v: str) -> str:
    for ch in v:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError("control characters are not allowed")
    return v


NoControl = AfterValidator(_no_control_chars)

URLStr = Annotated[str, Field(min_length=1, max_length=URL_MAX)]
SelectorStr = Annotated[str, Field(min_length=1, max_length=SELECTOR_MAX)]


class CaptureMode(str, Enum):  # noqa: UP042
    """Per-tab network capture behavior.

    NO_COPY (default): no body buffering; bodies fetched lazily from Chrome
    and pruned on main-frame navigation. Today's behavior.

    COPY: eagerly buffer request/response headers + bodies on loadingFinished.
    Still pruned on main-frame navigation.

    COPY_AND_RETAIN: like COPY, but resources are retained across navigations
    for the lifetime of the tab. Caller accepts unbounded growth.
    """

    NO_COPY = "no_copy"
    COPY = "copy"
    COPY_AND_RETAIN = "copy_and_retain"


class Cookie(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=COOKIE_NAME_MAX), NoControl]
    value: Annotated[str, Field(max_length=COOKIE_VALUE_MAX), NoControl]
    domain: Annotated[str | None, Field(default=None, max_length=COOKIE_DOMAIN_MAX), NoControl] = (
        None
    )
    path: Annotated[str | None, Field(default=None, max_length=COOKIE_PATH_MAX), NoControl] = None
    expires: float | None = None
    http_only: bool | None = Field(default=None, alias="httpOnly")
    secure: bool | None = None
    same_site: Literal["Strict", "Lax", "None"] | None = Field(default=None, alias="sameSite")

    model_config = {"populate_by_name": True}


class CreateTabRequest(BaseModel):
    url: URLStr
    cookies: list[Cookie] | None = None
    ttl_seconds: int | None = None
    capture_mode: CaptureMode = CaptureMode.NO_COPY


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
    state: Literal["in_progress", "completed", "canceled"]
    bytes_received: int
    size_bytes: int | None = None
    started_at: float
    completed_at: float | None


class CreateTabResponse(BaseModel):
    id: int
    status: int
    final_url: str
    content_type: str | None = None
    download: DownloadInfo | None = None


class FetchRequest(BaseModel):
    url: URLStr
    cookies: list[Cookie] | None = None
    ttl_seconds: int | None = None


class FetchResponse(BaseModel):
    status: int
    final_url: str
    html: str
    content_type: str | None = None


class GotoRequest(BaseModel):
    url: URLStr


class GotoResponse(BaseModel):
    status: int
    final_url: str
    content_type: str | None = None
    download: DownloadInfo | None = None


class ClickRequest(BaseModel):
    selector: SelectorStr


class TypeRequest(BaseModel):
    selector: SelectorStr
    text: Annotated[str, Field(max_length=TEXT_MAX)]


class EvalRequest(BaseModel):
    js: Annotated[str, Field(min_length=1, max_length=JS_MAX)]
    world: Literal["main", "isolated"] = "main"


class EvalResponse(BaseModel):
    result: Any


class WaitRequest(BaseModel):
    selector: Annotated[str | None, Field(default=None, max_length=SELECTOR_MAX)] = None
    network_idle: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=0, le=60_000)


class ResourceSummary(BaseModel):
    request_id: str
    url: str
    status: int
    mime_type: str
    resource_type: str
    encoded_size: int


class BrowserInfo(BaseModel):
    running: bool
    tab_count: int
    headless: bool
    shared_profile: bool
    extension_dirs: list[str]
    chrome_path: str | None = None


class BrowserShutdownResponse(BaseModel):
    ok: bool
    stopped: bool  # True iff Chromium was running and we just stopped it


class HealthResponse(BaseModel):
    ok: bool


class ErrorBody(BaseModel):
    error: str
    detail: str
