from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Type

from pydantic import BaseModel, ValidationError


class ToolNotFound(ValueError):
    pass


class ToolExecutionError(RuntimeError):
    pass


ToolHandler = Callable[[BaseModel], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: Type[BaseModel]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Tool names must be unique")

    @classmethod
    def combine(cls, *registries: "ToolRegistry") -> "ToolRegistry":
        return cls(
            [
                tool
                for registry in registries
                for tool in registry._tools.values()
            ]
        )

    def openai_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.arguments.model_json_schema(),
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFound(f"Tool '{name}' is not available")
        validated = tool.arguments.model_validate(arguments)
        return await tool.handler(validated)
