"""Проверка и изменение настроек пространства (`/space_config`).

Принимает только то, что остальной код сможет прочитать: показатели и отчёты проходят те же
`parse_metrics`/`parse_specs`, что и при работе, а перекрёстные ссылки (отчёт → показатель)
проверяются на итоговом наборе. Ничего не применяется «частично»: есть замечания — запись отклоняется.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

import pytz

from app.spaces import metrics, reports
from app.spaces.terms import TERMS

SETTING_KEYS = ("metrics", "reports", "auto_confirm", "intake", "categories", "terms")
COLUMN_KEYS = ("timezone", "currency")
PROPOSAL_TYPES = ("reminder", "shopping", "expense", "record")
MAX_BYTES = 8192


class ConfigError(ValueError):
    """Значение не годится; текст — для человека."""


def _size(settings: Dict[str, Any]) -> int:
    return len(json.dumps(settings, ensure_ascii=False).encode())


def _check_cross(settings: Dict[str, Any]) -> None:
    """Отчёты должны ссылаться на описанные показатели."""
    raw = settings.get("reports")
    if raw is None:
        return
    known = {metrics.EXPENSE, *(m.key for m in metrics.parse_metrics(settings.get("metrics"))[0])}
    specs, _ = reports.parse_specs(raw)
    for s in specs:
        if s.type == "metric" and s.metric not in known:
            raise ConfigError(f"отчёт «{s.id}» ссылается на показатель «{s.metric}», которого нет в metrics")


def validate_setting(key: str, value: Any) -> Any:
    """Проверить значение одного ключа настроек; вернуть нормализованное."""
    if key == "metrics":
        if not isinstance(value, list):
            raise ConfigError("metrics — список объектов {key,title,unit,kind[,categories]}")
        _, errs = metrics.parse_metrics(value)
        if errs:
            raise ConfigError("; ".join(errs[:5]))
        return value
    if key == "reports":
        if not isinstance(value, list):
            raise ConfigError("reports — список объектов")
        _, errs = reports.parse_specs(value)
        if errs:
            raise ConfigError("; ".join(errs[:5]))
        return value
    if key == "auto_confirm":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ConfigError("auto_confirm — список строк")
        bad = [v for v in value if v not in PROPOSAL_TYPES]
        if bad:
            raise ConfigError(f"неизвестные типы: {', '.join(bad[:5])}. Допустимо: {', '.join(PROPOSAL_TYPES)}")
        return list(dict.fromkeys(value))
    if key == "intake":
        if not isinstance(value, bool):
            raise ConfigError("intake — true или false")
        return value
    if key == "categories":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ConfigError("categories — список строк")
        names = list(dict.fromkeys(" ".join(v.split()) for v in value if v.strip()))
        if not names:
            raise ConfigError("categories пуст — чтобы вернуть стандартные, используйте unset")
        if len(names) > metrics.MAX_CATEGORIES or any(len(n) > 50 for n in names):
            raise ConfigError(f"не больше {metrics.MAX_CATEGORIES} категорий, имя до 50 символов")
        return names
    if key == "terms":
        allowed = set(TERMS["generic"])
        if not isinstance(value, dict):
            raise ConfigError("terms — объект {ключ: подпись}")
        bad = [k for k in value if k not in allowed]
        if bad:
            raise ConfigError(f"неизвестные ключи: {', '.join(map(str, bad[:5]))}. Допустимо: {', '.join(sorted(allowed))}")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 40 for v in value.values()):
            raise ConfigError("подписи — непустые строки до 40 символов")
        return {k: " ".join(v.split()) for k, v in value.items()}
    raise ConfigError(f"неизвестный ключ «{key}». Доступно: {', '.join(SETTING_KEYS + COLUMN_KEYS)}")


def validate_column(key: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} — строка")
    v = value.strip()
    if key == "timezone":
        if v not in pytz.all_timezones_set:
            raise ConfigError("неизвестный часовой пояс (пример: Europe/Moscow)")
        return v
    if not re.fullmatch(r"[A-Za-z]{3}", v):
        raise ConfigError("валюта — три буквы (пример: RUB)")
    return v.upper()


def prepare(current: Dict[str, Any], key: str, value: Any) -> Dict[str, Any]:
    """Итоговые settings после `set key value`; ConfigError, если что-то не сходится или слишком велико."""
    candidate = dict(current or {})
    candidate[key] = validate_setting(key, value)
    if key in ("metrics", "reports"):
        _check_cross(candidate)
    if _size(candidate) > MAX_BYTES:
        raise ConfigError(f"настройки больше {MAX_BYTES} байт")
    return candidate


def prepare_unset(current: Dict[str, Any], key: str) -> Dict[str, Any]:
    if key not in SETTING_KEYS:
        raise ConfigError(f"сбрасывать можно: {', '.join(SETTING_KEYS)}")
    candidate = dict(current or {})
    candidate.pop(key, None)
    if key == "metrics":
        _check_cross(candidate)
    return candidate


def parse_command(args: str) -> Tuple[str, str, str]:
    """`set key json` / `unset key` → (action, key, rest); пустая строка — показать."""
    parts = (args or "").strip().split(None, 2)
    if not parts:
        return "show", "", ""
    action = parts[0].lower()
    if action not in ("set", "unset") or len(parts) < 2 or (action == "set" and len(parts) < 3):
        raise ConfigError("формат: /space_config set <ключ> <JSON> или /space_config unset <ключ>")
    return action, parts[1].lower(), parts[2] if len(parts) > 2 else ""


def load_value(key: str, raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        if key in COLUMN_KEYS:
            return raw.strip()
        raise ConfigError("не JSON: проверьте кавычки и скобки") from None


def render(settings: Dict[str, Any], limit: int = 3500) -> str:
    text = json.dumps(settings or {}, ensure_ascii=False, indent=1)
    return text if len(text) <= limit else text[:limit] + "\n…"


__all__: List[str] = ["ConfigError", "prepare", "prepare_unset", "validate_column", "parse_command", "load_value", "render"]
