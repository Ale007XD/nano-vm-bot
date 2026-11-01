"""Подписи интерфейса по виду пространства: в коде нет слов «семья», «класс» и т. п."""
from __future__ import annotations

from typing import Any, Dict, Optional

DEFAULT_KIND = "generic"

TERMS: Dict[str, Dict[str, str]] = {
    "generic": {"space": "Группа", "member": "участник", "members": "Участники",
                "ledger": "Расходы", "checklist": "Список", "task": "Задача"},
    "family": {"space": "Семья", "member": "член семьи", "members": "Семья",
               "ledger": "Расходы", "checklist": "Покупки", "task": "Дело"},
    "company": {"space": "Фирма", "member": "сотрудник", "members": "Сотрудники",
                "ledger": "Затраты", "checklist": "Список", "task": "Задача"},
    "class": {"space": "Класс", "member": "ученик", "members": "Ученики",
              "ledger": "Взносы", "checklist": "Задания", "task": "Задание"},
}


def terms_for(kind: Optional[str], settings: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Подписи вида; неизвестный вид — generic; `settings["terms"]` пространства переопределяет."""
    result = dict(TERMS[DEFAULT_KIND])
    result.update(TERMS.get(kind or "", {}))
    override = (settings or {}).get("terms")
    if isinstance(override, dict):
        result.update({k: v for k, v in override.items() if isinstance(v, str) and v.strip()})
    return result


def term(kind: Optional[str], key: str, settings: Optional[Dict[str, Any]] = None) -> str:
    return terms_for(kind, settings).get(key, key)
