"""HTTP v1 input schemas; domain results reuse core contracts."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRequest(RequestModel):
    prompt: str = Field(min_length=1, max_length=100000)
    branch_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)


class BrowserRequest(RequestModel):
    arguments: dict
    branch_id: str | None = None


class MCPRequest(RequestModel):
    servers: dict


class MCPConnectRequest(RequestModel):
    disconnect: bool = False


class ForkRequest(RequestModel):
    at_event: int | None = Field(default=None, ge=0)


class RestoreRequest(RequestModel):
    event_id: int = Field(ge=1)


class SettingsRequest(RequestModel):
    workspace: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str | None = None
    clear_api_key: bool = False
    context_tokens: int = Field(default=32768, ge=1024)
    max_output_tokens: int = Field(default=8192, ge=1)
    reasoning_effort: str | None = "none"
    temperature: float = Field(default=0.1, ge=0, le=2)
    request_timeout_seconds: float = Field(default=180, gt=0)
    run_timeout_seconds: float = Field(default=600, gt=0)
    auto_compact: bool = True
    compact_threshold: float = Field(default=0.9, gt=0, lt=1)
    compact_target: float = Field(default=0.6, gt=0, lt=1)
    auto_continue: bool = True
    repeated_failure_limit: int = Field(default=2, ge=1)
    recovery_blocked_rounds: int = Field(default=2, ge=1)
    recovery_timeout_seconds: float = Field(default=600, gt=0)
    recovery_output_tokens: int = Field(default=32768, ge=1)
    unchanged_result_limit: int = Field(default=4, ge=2)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Use an HTTP(S) base URL without embedded credentials")
        return value.rstrip("/")

    @field_validator("reasoning_effort")
    @classmethod
    def valid_effort(cls, value: str | None) -> str | None:
        if value not in {None, "none", "low", "medium", "high", "xhigh"}:
            raise ValueError("Unsupported reasoning effort")
        return value
