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
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        try:
            Draft202012Validator(tool.spec.parameters).validate(arguments)
        except ValidationError as exc:
            return ToolResult(f"Invalid arguments: {exc.message}", is_error=True)
        try:
            return await tool.execute(arguments, context)
        except Exception as exc:
            context.emit("tool.exception", {"type": type(exc).__name__, "detail": str(exc)})
            return ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)
