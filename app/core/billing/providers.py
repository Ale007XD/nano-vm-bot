"""
Платёжные провайдеры.

Все три — через Telegram Payments (один и тот же send_invoice):
  • Telegram Stars (XTR): provider_token пустой, возврат через refund_star_payment;
  • ЮKassa (RUB) и Stripe (USD): provider_token выдаёт @BotFather (Payments),
    возврат — вручную в кабинете провайдера.
Новый провайдер = подкласс PaymentProvider; прикладной код знает только интерфейс.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Dict, List

from aiogram.types import LabeledPrice

from app.core.billing.packages import Package

logger = logging.getLogger(__name__)


class PaymentProvider(ABC):
    name: str
    label: str
    currency: str

    @abstractmethod
    async def send_invoice(self, bot, chat_id: int, user_id: int, package: Package) -> None: ...

    @abstractmethod
    async def refund(self, bot, user_id: int, charge_id: str) -> bool:
        """True — деньги возвращены автоматически; False — возврат делается вручную."""

    @staticmethod
    def make_payload(provider: str, package_id: str, user_id: int) -> str:
        return f"buy:{provider}:{package_id}:{user_id}"

    @staticmethod
    def parse_payload(payload: str):
        """-> (provider, package_id, user_id) или None."""
        parts = (payload or "").split(":")
        if len(parts) != 4 or parts[0] != "buy" or not parts[3].lstrip("-").isdigit():
            return None
        return parts[1], parts[2], int(parts[3])


class TelegramInvoiceProvider(PaymentProvider):
    def __init__(self, name: str, label: str, currency: str, provider_token: str = ""):
        self.name, self.label, self.currency = name, label, currency
        self._token = provider_token

    async def send_invoice(self, bot, chat_id: int, user_id: int, package: Package) -> None:
        amount = package.price(self.currency)
        if amount is None:
            raise ValueError(f"package {package.id} has no price in {self.currency}")
        await bot.send_invoice(
            chat_id=chat_id,
            title=package.title[:32],
            description=f"{package.credits} сообщений сверх бесплатного суточного лимита"[:255],
            payload=self.make_payload(self.name, package.id, user_id),
            provider_token=self._token,
            currency=self.currency,
            prices=[LabeledPrice(label=package.title[:32], amount=amount)],
        )

    async def refund(self, bot, user_id: int, charge_id: str) -> bool:
        return False


class StarsProvider(TelegramInvoiceProvider):
    def __init__(self):
        super().__init__("stars", "⭐ Telegram Stars", "XTR", "")

    async def refund(self, bot, user_id: int, charge_id: str) -> bool:
        return bool(await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id))


def build_providers(settings) -> Dict[str, PaymentProvider]:
    """Включённые провайдеры из BILLING_PROVIDERS (через запятую; fiat — только с токеном)."""
    wanted: List[str] = [
        p.strip().lower() for p in str(getattr(settings, "BILLING_PROVIDERS", "") or "").split(",") if p.strip()
    ]
    out: Dict[str, PaymentProvider] = {}
    for name in wanted:
        if name == "stars":
            out[name] = StarsProvider()
        elif name == "yookassa":
            tok = getattr(settings, "YOOKASSA_PROVIDER_TOKEN", "") or ""
            if tok:
                out[name] = TelegramInvoiceProvider("yookassa", "💳 Карта РФ (ЮKassa)", "RUB", tok)
            else:
                logger.error("BILLING_PROVIDERS has yookassa but YOOKASSA_PROVIDER_TOKEN is empty — skipped")
        elif name == "stripe":
            tok = getattr(settings, "STRIPE_PROVIDER_TOKEN", "") or ""
            if tok:
                out[name] = TelegramInvoiceProvider("stripe", "💳 Карта (Stripe)", "USD", tok)
            else:
                logger.error("BILLING_PROVIDERS has stripe but STRIPE_PROVIDER_TOKEN is empty — skipped")
        else:
            logger.error("unknown billing provider %r ignored", name)
    return out


def billing_enabled(settings=None) -> bool:
    if settings is None:
        from app.core.config import settings as s

        settings = s
    return bool(build_providers(settings))
