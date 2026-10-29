"""
skills_loader.py — два независимых класса с чёткими границами ответственности.

SkillLoader      — файловая система: load, list, create, delete, get_code.
OpenClawExecutor — исполнитель: allowlist + Pydantic-first валидация.

Публичные singletons:
    skill_loader  — экземпляр SkillLoader  (bot.py, handlers/skills.py)
    SkillLoader   — алиас класса (обратная совместимость)
"""

import importlib.util
import inspect
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """Ошибка исполнения навыка. Возвращается как значение, не бросается."""
    def __init__(self, message: str, error_code: str = "UNKNOWN"):
        super().__init__(message)
        self.error_code = error_code


@dataclass
class SkillInfo:
    name: str
    description: str
    category: str = "custom"
    source: str = "custom"
    version: str = "1.0.0"
    author: str = "unknown"
    commands: List[str] = field(default_factory=list)
    is_active: bool = True
    file_path: Optional[str] = None
    callable: Optional[Callable] = None
    # kind="py" — исполняемый навык (run(context)); kind="skillmd" —
    # инструкционный навык формата Agent Skills (SKILL.md), исполняет LLM
    kind: str = "py"
    instructions: Optional[str] = None


# ---------------------------------------------------------------------------
# SkillLoader — файловая система
# ---------------------------------------------------------------------------

_SKILLS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "skills"
)
_SKILL_DIRS = {
    "system":   os.path.join(_SKILLS_ROOT, "system"),
    "custom":   os.path.join(_SKILLS_ROOT, "custom"),
    "external": os.path.join(_SKILLS_ROOT, "external"),
}


class SkillLoader:
    """
    Реестр скиллов: загружает .py из skills/{system,custom,external}/,
    предоставляет CRUD-методы и интроспекцию.
    """

    def __init__(self):
        self._skills: Dict[str, SkillInfo] = {}
        self._modules: Dict[str, Any] = {}

    @property
    def skills(self) -> Dict[str, SkillInfo]:
        """Публичный доступ к реестру скиллов (read-only view)."""
        return self._skills

    @property
    def skill_info(self) -> Dict[str, SkillInfo]:
        """Алиас skills — обратная совместимость с test_bot.py."""
        return self._skills

    # ------------------------------------------------------------------
    # Загрузка при старте
    # ------------------------------------------------------------------

    async def load_all_skills(self) -> int:
        """Обходит все директории скиллов. Возвращает количество загруженных."""
        loaded = 0
        for source, directory in _SKILL_DIRS.items():
            if not os.path.isdir(directory):
                continue
            for filename in sorted(os.listdir(directory)):
                path = os.path.join(directory, filename)
                # Инструкционные навыки Agent Skills: папка с SKILL.md
                if os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md")):
                    try:
                        info = self._load_skill_md_folder(path, source)
                        if info:
                            self._skills[info.name] = info
                            loaded += 1
                            logger.info("Skill loaded: %s (%s, SKILL.md)", info.name, source)
                    except Exception as e:
                        logger.warning("Failed to load SKILL.md skill %s: %s", filename, e)
                    continue
                if not filename.endswith(".py") or filename.startswith("_"):
                    continue
                try:
                    info = self._load_skill_file(path, source)
                    if info:
                        self._skills[info.name] = info
                        loaded += 1
                        logger.info("Skill loaded: %s (%s)", info.name, source)
                except Exception as e:
                    logger.warning("Failed to load skill %s: %s", filename, e)

        logger.info("Skills loaded: %d total", loaded)
        return loaded

    # ------------------------------------------------------------------
    # SKILL.md (Agent Skills, agentskills.io)
    # ------------------------------------------------------------------

    @staticmethod
    def parse_skill_md(text: str) -> dict:
        """SKILL.md → {"name", "description", "instructions"}.

        Frontmatter (YAML между --- маркерами) опционален; name/description
        при отсутствии берутся по месту (имя папки / первая строка).
        """
        lines = text.splitlines()
        meta: Dict[str, Any] = {}
        body_start = 0
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() in ("---", "..."):
                    fm_text = "\n".join(lines[1:i])
                    meta = SkillLoader._parse_frontmatter(fm_text)
                    body_start = i + 1
                    break

        instructions = "\n".join(lines[body_start:]).strip()
        return {"meta": meta, "instructions": instructions}

    @staticmethod
    def _parse_frontmatter(fm_text: str) -> Dict[str, Any]:
        try:
            import yaml

            data = yaml.safe_load(fm_text)
            return data if isinstance(data, dict) else {}
        except ImportError:
            # fallback без PyYAML: простые "key: value" строки
            result: Dict[str, Any] = {}
            for line in fm_text.splitlines():
                if ":" in line and not line.startswith((" ", "\t", "-")):
                    key, _, value = line.partition(":")
                    result[key.strip()] = value.strip().strip("\"'")
            return result

    def _load_skill_md_folder(self, skill_dir: str, source: str) -> Optional[SkillInfo]:
        """Загружает навык из папки с SKILL.md (инструкционный, исполняет LLM)."""
        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_md_path, encoding="utf-8") as fh:
            text = fh.read()

        parsed = self.parse_skill_md(text)
        meta = parsed["meta"]
        instructions = parsed["instructions"]
        folder_name = os.path.basename(os.path.normpath(skill_dir))

        name = str(meta.get("name") or folder_name).strip()
        first_line = instructions.splitlines()[0][:200] if instructions else ""
        description = str(meta.get("description") or first_line).strip() or f"External skill {name}"

        if not instructions:
            raise ValueError("SKILL.md has no instructions body")

        return SkillInfo(
            name=name,
            description=description,
            category=str(meta.get("category") or "external-skill"),
            source=source,
            version=str(meta.get("version") or "1.0.0"),
            author=str(meta.get("author") or "external"),
            commands=[],
            is_active=True,
            file_path=skill_md_path,
            callable=None,
            kind="skillmd",
            instructions=instructions,
        )

    def install_skill_md(self, name_hint: str, content: str) -> SkillInfo:
        """Сохраняет присланный SKILL.md в skills/external/<name>/ и загружает.

        Возвращает SkillInfo; ValueError — если файл не выглядит как SKILL.md.
        """
        parsed = self.parse_skill_md(content)
        meta = parsed["meta"]
        instructions = parsed["instructions"]

        name = str(meta.get("name") or name_hint).strip()
        if not is_valid_skill_name(name):
            name = re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_") or "external_skill"
        if not instructions:
            raise ValueError("нет блока инструкций после frontmatter")

        skill_dir = os.path.join(_SKILL_DIRS["external"], name)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(content)

        info = self._load_skill_md_folder(skill_dir, "external")
        self._skills[info.name] = info
        logger.info("SKILL.md skill installed: %s", info.name)
        return info

    def _load_skill_file(self, path: str, source: str) -> Optional[SkillInfo]:
        """Импортирует .py как модуль и извлекает метаданные."""
        base = os.path.splitext(os.path.basename(path))[0]
        module_name = f"_skill_{base}"

        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        name        = getattr(module, "SKILL_NAME",        base)
        description = getattr(module, "SKILL_DESCRIPTION", f"Skill: {name}")
        category    = getattr(module, "SKILL_CATEGORY",    source)
        version     = getattr(module, "SKILL_VERSION",     "1.0.0")
        author      = getattr(module, "SKILL_AUTHOR",      "unknown")
        commands    = list(getattr(module, "SKILL_COMMANDS", []))

        entry = getattr(module, "run", None) or getattr(module, "handle_command", None)

        self._modules[name] = module

        return SkillInfo(
            name=name,
            description=description,
            category=category,
            source=source,
            version=version,
            author=author,
            commands=commands,
            is_active=True,
            file_path=path,
            callable=entry,
        )

    # ------------------------------------------------------------------
    # Чтение / поиск
    # ------------------------------------------------------------------

    def list_skills(self) -> List[SkillInfo]:
        return list(self._skills.values())

    _SEARCH_STOPWORDS = frozenset({
        "find", "search", "look", "for", "skill", "skills", "the", "a", "an",
        "найди", "найти", "поищи", "поиск", "ищи", "навык", "навыки", "навыков", "про", "для",
    })

    def search_skills(self, query: str) -> List[SkillInfo]:
        """Поиск среди загруженных навыков по имени, описанию, категории и командам.

        Служебные слова запроса («найди навык», "search for") отбрасываются; навык
        подходит, если содержит хотя бы одно из оставшихся слов. Чем больше слов
        совпало — тем выше в выдаче. Пустой запрос — пусто.
        """
        terms = [
            t for t in (query or "").lower().split()
            if t and t not in self._SEARCH_STOPWORDS
        ]
        if not terms:
            return []
        scored = []
        for info in self._skills.values():
            hay = " ".join(
                [info.name, info.description or "", info.category or "", *info.commands]
            ).lower()
            hits = sum(1 for t in terms if t in hay)
            if hits:
                scored.append((hits, info))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [info for _, info in scored]

    def get_skill_info(self, name: str) -> Optional[SkillInfo]:
        return self._skills.get(name)

    def get_skill(self, name: str) -> Optional[Any]:
        info = self._skills.get(name)
        return info.callable if info else None

    async def get_skill_code(self, name: str) -> Optional[str]:
        info = self._skills.get(name)
        if info and info.file_path and os.path.isfile(info.file_path):
            try:
                with open(info.file_path, "r", encoding="utf-8") as fh:
                    return fh.read()
            except OSError as e:
                logger.warning("Cannot read skill file %s: %s", info.file_path, e)
        return None

    def get_all_tool_schemas(self) -> List[Dict[str, Any]]:
        """MCP Tools Primitive — схемы всех активных навыков."""
        return [
            {
                "name": info.name,
                "description": info.description,
                "parameters": {"type": "object", "properties": {}, "required": []},
            }
            for info in self._skills.values()
            if info.is_active and info.callable
        ]

    # ------------------------------------------------------------------
    # Создание / удаление
    # ------------------------------------------------------------------

    async def create_skill(
        self,
        name: str,
        description: str,
        code: str,
        category: str = "custom",
        author: str = "user",
    ) -> SkillInfo:
        if not is_valid_skill_name(name):
            raise ValueError(f"Invalid skill name: {name!r}")
        custom_dir = _SKILL_DIRS["custom"]
        os.makedirs(custom_dir, exist_ok=True)
        file_path = os.path.join(custom_dir, f"{name}.py")

        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(code)

        info = None
        try:
            info = self._load_skill_file(file_path, "custom")
        except Exception as e:
            logger.warning("Could not import created skill %s: %s", name, e)

        if info is None:
            info = SkillInfo(
                name=name,
                description=description,
                category=category,
                source="custom",
                author=author,
                file_path=file_path,
            )

        info.description = description or info.description
        info.author = author or info.author
        self._skills[name] = info
        logger.info("Skill created: %s", name)
        return info

    async def delete_skill(self, name: str) -> None:
        info = self._skills.get(name)
        if info is None:
            raise ValueError(f"Skill '{name}' not found")
        if info.source == "system":
            raise PermissionError("Cannot delete system skill")

        if info.file_path and os.path.isfile(info.file_path):
            os.remove(info.file_path)

        self._skills.pop(name, None)
        self._modules.pop(name, None)
        logger.info("Skill deleted: %s", name)


# ---------------------------------------------------------------------------
# OpenClawExecutor — исполнитель (без файловой системы)
# ---------------------------------------------------------------------------

class OpenClawExecutor:
    """
    Безопасный исполнитель навыков.
    Allowlist по публичным функциям, Pydantic-first валидация.
    Не знает о файловой системе и о SkillLoader.
    """

    def __init__(self):
        self._registry: Dict[str, Callable] = {}
        self._allowlist: Dict[str, bool] = {}

    def register(self, func: Callable) -> Callable:
        name = func.__name__
        self._registry[name] = func
        self._allowlist[name] = True
        return func

    def get_tool_schema(self, tool: Union[Callable, str]) -> Dict[str, Any]:
        """
        Генерирует JSON Schema.
        Если первый параметр — Pydantic BaseModel, использует model_json_schema().
        Иначе — интроспекция сигнатуры.
        """
        if isinstance(tool, str):
            if tool not in self._registry:
                raise ToolError(f"Tool '{tool}' not found", error_code="NOT_FOUND")
            func = self._registry[tool]
        else:
            func = tool

        sig = inspect.signature(func)
        params = [
            p for pname, p in sig.parameters.items()
            if pname not in ("self", "cls")
            and p.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        ]

        if params:
            annotation = params[0].annotation
            if annotation is not inspect.Parameter.empty:
                try:
                    from pydantic import BaseModel
                    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                        schema = annotation.model_json_schema()
                        return {
                            "name": func.__name__,
                            "description": func.__doc__ or f"Execute {func.__name__}",
                            "parameters": {
                                "type": "object",
                                "properties": schema.get("properties", {}),
                                "required": schema.get("required", []),
                            },
                        }
                except ImportError:
                    pass

        properties: Dict[str, Any] = {}
        required: List[str] = []
        for param in params:
            properties[param.name] = {"type": "string"}
            if param.default is inspect.Parameter.empty:
                required.append(param.name)

        return {
            "name": func.__name__,
            "description": func.__doc__ or f"Execute {func.__name__}",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    async def execute(self, name: str, params: dict) -> Any:
        """Исполняет навык. При нарушении allowlist возвращает ToolError."""
        if name.startswith("_") or name not in self._allowlist:
            return ToolError(
                f"Security Policy Violation: access denied to '{name}'",
                error_code="ACCESS_DENIED",
            )
        func = self._registry[name]
        if inspect.iscoroutinefunction(func):
            return await func(**params)
        return func(**params)


# ---------------------------------------------------------------------------
# MCP / Facade (P-mcp, заглушки)
# ---------------------------------------------------------------------------

class MCPClientExecutorDirect:
    """Заглушка. Ожидает реализации изоляции (sandbox/Wasmtime)."""
    async def execute(self, name: str, params: dict) -> Any:
        raise NotImplementedError("MCPClientExecutorDirect not yet implemented")


class SkillLoaderFacade:
    """Реестр экзекуторов: fast-track (system) и isolated-track (external)."""
    def __init__(self):
        self._system = OpenClawExecutor()
        self._external = MCPClientExecutorDirect()

    def register_system_skill(self, func: Callable) -> Callable:
        return self._system.register(func)

    async def execute(self, track: str, name: str, params: dict) -> Any:
        if track == "system":
            return await self._system.execute(name, params)
        return await self._external.execute(name, params)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_valid_skill_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name))


# ---------------------------------------------------------------------------
# Singletons / public exports
# ---------------------------------------------------------------------------

skill_loader = SkillLoader()
