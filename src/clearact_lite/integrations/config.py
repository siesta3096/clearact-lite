"""One MCP configuration schema for UI, CLI and session adapters."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    transport: Literal["stdio", "streamable_http", "sse"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    connect_timeout: float = Field(default=15, gt=0)
    call_timeout: float = Field(default=60, gt=0)

    @model_validator(mode="after")
    def valid_transport(self):
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio requires a command")
        if self.transport != "stdio":
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("HTTP/SSE requires an HTTP(S) URL")
        return self


def normalize_servers(raw: dict, previous: dict | None = None) -> dict:
    if "mcpServers" in raw:
        raw = raw["mcpServers"]
    if not isinstance(raw, dict):
        raise ValueError("MCP configuration must be an object")
    result = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
            raise ValueError("Each MCP server requires a name and object configuration")
        data = dict(value)
        data.setdefault("transport", "stdio" if data.get("command") else "streamable_http")
        for secret in ("env", "headers"):
            if data.get(secret) is None:
                data[secret] = (previous or {}).get(name, {}).get(secret, {})
        for old, new in (("connectTimeoutSeconds", "connect_timeout"), ("callTimeoutSeconds", "call_timeout")):
            if old in data:
                data[new] = data.pop(old)
        try:
            result[name] = MCPServerConfig.model_validate(data).model_dump()
        except ValueError:
            # Configuration errors must not echo headers/env or other credentials.
            raise ValueError(f"MCP 配置 {name!r} 不完整，请检查连接方式、地址、命令和超时") from None
    return result
