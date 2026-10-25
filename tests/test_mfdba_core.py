from typing import Literal

import pytest
from pydantic import BaseModel, Field

from app.core.skills_loader import OpenClawExecutor, ToolError


# Literal['metric', 'imperial'] → Pydantic генерирует "enum" в JSON Schema,
# что и проверяет test_pydantic_schema_generation.
# Field(pattern=...) генерирует только "pattern" — enum там нет.
class WeatherArgs(BaseModel):
    city: str = Field(..., description="Название города")
    units: Literal["metric", "imperial"] = "metric"


def get_weather(args: WeatherArgs):
    return f"Weather in {args.city}"


@pytest.mark.asyncio
async def test_pydantic_schema_generation():
    """Проверка генерации схемы для LLM через .model_json_schema()"""
    executor = OpenClawExecutor()
    # get_tool_schema принимает callable напрямую — без register_module
    schema = executor.get_tool_schema(get_weather)

    assert schema["name"] == "get_weather"
    assert "city" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["city"]
    assert "enum" in schema["parameters"]["properties"]["units"]


@pytest.mark.asyncio
async def test_security_access_denied():
    """Проверка блокировки несанкционированного доступа (Allowlist)"""
    executor = OpenClawExecutor()

    # Попытка вызова приватного/дандер метода
    result = await executor.execute("__init__", {})
    assert isinstance(result, ToolError)
    assert result.error_code == "ACCESS_DENIED"
