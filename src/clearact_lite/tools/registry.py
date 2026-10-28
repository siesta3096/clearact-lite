import json
import re

from jsonschema import Draft202012Validator, ValidationError

from clearact_lite.core.contracts import ToolResult, ToolSpec
from clearact_lite.core.ports import AsyncResource, Tool, ToolContext


class ToolRegistry:
    def __init__(self, tools: list[Tool], resources: list[AsyncResource] | None = None):
        self.resources = resources or []
        self.tools: dict[str, Tool] = {}
        for tool in tools:
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.spec.name) is None:
                raise ValueError(f"Invalid function name: {tool.spec.name}")
            if tool.spec.name in self.tools:
                raise ValueError(f"Duplicate tool: {tool.spec.name}")
            Draft202012Validator.check_schema(tool.spec.parameters)
            self.tools[tool.spec.name] = tool

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools.values()]

    async def aclose(self) -> None:
        for resource in reversed(self.resources):
            await resource.aclose()

    async def execute(self, name: str, arguments: dict, context: ToolContext) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}", is_error=True, error_kind="validation")
        try:
            Draft202012Validator(tool.spec.parameters).validate(arguments)
        except ValidationError as exc:
            return ToolResult(f"Invalid arguments: {exc.message}", is_error=True, error_kind="validation")
        try:
            return await tool.execute(arguments, context)
        except Exception as exc:
            diagnostic = getattr(exc, "browser_diagnostics", None)
            context.emit(
                "tool.exception",
                {
                    "type": type(exc).__name__,
                    "detail": str(exc),
                    **({"browser_diagnostics": diagnostic} if diagnostic else {}),
                },
            )
            kind = "unknown"
            if isinstance(exc, TypeError | AttributeError | NameError | SyntaxError | FileNotFoundError):
                kind = "deterministic"
            elif isinstance(exc, TimeoutError | ConnectionError) or "Timeout" in type(exc).__name__:
                kind = "transient"
            elif name == "browser_action" and arguments.get("action") == "evaluate":
                first_line = str(exc).splitlines()[0] if str(exc) else ""
                if any(tag in first_line for tag in ("TypeError:", "ReferenceError:", "SyntaxError:")):
                    kind = "deterministic"
            content = f"{type(exc).__name__}: {exc}"
            if diagnostic:
                content += "\nBrowser diagnostics: " + json.dumps(diagnostic, ensure_ascii=False)
            return ToolResult(content, is_error=True, error_kind=kind)
