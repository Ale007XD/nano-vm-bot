"""Каталог пакетов кредитов (настройка CREDIT_PACKAGES — JSON)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# цены в минимальных единицах валюты: XTR — звёзды, RUB — копейки, USD — центы
DEFAULT_PACKAGES_JSON = json.dumps(
    [
        {"id": "s50", "credits": 50, "title": "50 запросов",
         "prices": {"XTR": 100, "RUB": 19900, "USD": 249}},
        {"id": "s250", "credits": 250, "title": "250 запросов",
         "prices": {"XTR": 400, "RUB": 79900, "USD": 899}},
    ],
    ensure_ascii=False,
)


@dataclass(frozen=True)
class Package:
    id: str
    credits: int
    title: str
    prices: Dict[str, int] = field(default_factory=dict)

    def price(self, currency: str) -> Optional[int]:
        return self.prices.get(currency)


def _parse_one(raw) -> Package:
    pid = str(raw["id"])
    if not pid.replace("_", "").isalnum() or len(pid) > 32 or ":" in pid:
        raise ValueError(f"bad package id {pid!r}")
    credits = int(raw["credits"])
    if credits <= 0:
        raise ValueError("credits must be > 0")
    prices = {str(k).upper(): int(v) for k, v in dict(raw["prices"]).items()}
    if not prices or any(v <= 0 for v in prices.values()):
        raise ValueError("prices must be positive")
    title = str(raw.get("title") or f"{credits} запросов")[:32]
    return Package(id=pid, credits=credits, title=title, prices=prices)


def parse_packages(text: str) -> List[Package]:
    """Разобрать JSON; битые записи пропускаются, при полном провале — каталог по умолчанию."""
    out: List[Package] = []
    try:
        data = json.loads(text or "")
        if not isinstance(data, list):
            raise ValueError("not a list")
    except (ValueError, TypeError) as e:
        logger.error("CREDIT_PACKAGES is invalid JSON (%s) — using defaults", e)
        data = json.loads(DEFAULT_PACKAGES_JSON)
    seen = set()
    for raw in data:
        try:
            pkg = _parse_one(raw)
        except (KeyError, ValueError, TypeError) as e:
            logger.error("skipping bad package %r: %s", raw, e)
            continue
        if pkg.id in seen:
            logger.error("duplicate package id %s skipped", pkg.id)
            continue
        seen.add(pkg.id)
        out.append(pkg)
    return out


def get_packages() -> List[Package]:
    from app.core.config import settings

    raw = getattr(settings, "CREDIT_PACKAGES", "") or DEFAULT_PACKAGES_JSON
    return parse_packages(raw if isinstance(raw, str) else DEFAULT_PACKAGES_JSON)


def get_package(package_id: str) -> Optional[Package]:
    return next((p for p in get_packages() if p.id == package_id), None)
