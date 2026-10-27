"""
Учёт кредитов и платежей. Все операции атомарны; session_maker инжектируется.

- Зачисление идемпотентно: повторная доставка successful_payment (по уникальному
  (provider, charge_id)) не удваивает баланс.
- Списание — условный UPDATE (balance >= n): гонки между параллельными
  сообщениями не уводят баланс в минус.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core.billing.models import (
    PAID,
    REFUNDED,
    CreditAccount,
    CreditLedger,
    Payment,
)
from app.core.billing.packages import Package
from app.core.ext_db import resolve_session_maker


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _add_credits(s, telegram_id: int, n: int, reason: str, ref: str) -> None:
    """Увеличить баланс (апсерт) и записать в журнал. Вызывается внутри транзакции."""
    res = await s.execute(
        update(CreditAccount)
        .where(CreditAccount.telegram_id == telegram_id)
        .values(balance=CreditAccount.balance + n, updated_at=_now())
    )
    if not res.rowcount:
        await s.execute(insert(CreditAccount).values(telegram_id=telegram_id, balance=n, updated_at=_now()))
    s.add(CreditLedger(telegram_id=telegram_id, delta=n, reason=reason, ref=ref))


async def get_balance(telegram_id: int, session_maker=None) -> int:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = await s.get(CreditAccount, telegram_id)
        return int(row.balance) if row else 0


async def consume_credit(telegram_id: int, n: int = 1, session_maker=None) -> bool:
    """Списать n кредитов. False, если не хватает (баланс не меняется)."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        res = await s.execute(
            update(CreditAccount)
            .where(CreditAccount.telegram_id == telegram_id, CreditAccount.balance >= n)
            .values(balance=CreditAccount.balance - n, updated_at=_now())
        )
        if not res.rowcount:
            await s.rollback()
            return False
        s.add(CreditLedger(telegram_id=telegram_id, delta=-n, reason="spend", ref=""))
        await s.commit()
        return True


async def record_payment(
    telegram_id: int,
    package: Package,
    provider: str,
    charge_id: str,
    amount: int,
    currency: str,
    session_maker=None,
) -> Optional[Payment]:
    """Зачислить оплаченный пакет. None — платёж уже учтён (повторная доставка)."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        pay = Payment(
            provider=provider, charge_id=charge_id, telegram_id=telegram_id,
            package_id=package.id, credits=package.credits, amount=amount,
            currency=currency, status=PAID,
        )
        s.add(pay)
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            return None
        await _add_credits(s, telegram_id, package.credits, "purchase", str(pay.id))
        await s.commit()
        return pay


async def grant_credits(telegram_id: int, n: int, reason: str = "grant", session_maker=None) -> int:
    """Начислить n>0 кредитов вручную (админ). Возвращает новый баланс."""
    if n <= 0:
        raise ValueError("n must be positive")
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        await _add_credits(s, telegram_id, n, reason, "")
        await s.commit()
    return await get_balance(telegram_id, session_maker)


async def get_payment(payment_id: int, session_maker=None) -> Optional[Payment]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        return await s.get(Payment, payment_id)


async def recent_payments(telegram_id: int, limit: int = 5, session_maker=None) -> List[Payment]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        rows = await s.execute(
            select(Payment).where(Payment.telegram_id == telegram_id)
            .order_by(Payment.id.desc()).limit(limit)
        )
        return list(rows.scalars())


async def reverse_payment(payment_id: int, session_maker=None) -> str:
    """
    Откатить платёж: снять кредиты и пометить refunded (деньги возвращает вызывающий).

    Возвращает 'ok' | 'not_found' | 'already_refunded' | 'credits_spent'.
    Если кредиты уже потрачены (баланс < credits) — откат отказывается: иначе
    «купил → потратил → вернул деньги» даёт бесплатное использование.
    """
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        pay = await s.get(Payment, payment_id)
        if pay is None:
            return "not_found"
        if pay.status == REFUNDED:
            return "already_refunded"
        res = await s.execute(
            update(CreditAccount)
            .where(CreditAccount.telegram_id == pay.telegram_id, CreditAccount.balance >= pay.credits)
            .values(balance=CreditAccount.balance - pay.credits, updated_at=_now())
        )
        if not res.rowcount:
            await s.rollback()
            return "credits_spent"
        pay.status = REFUNDED
        s.add(CreditLedger(telegram_id=pay.telegram_id, delta=-pay.credits, reason="refund", ref=str(pay.id)))
        await s.commit()
        return "ok"


async def restore_payment(payment_id: int, session_maker=None) -> None:
    """Компенсация: возврат денег у провайдера не удался — вернуть кредиты и статус."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        pay = await s.get(Payment, payment_id)
        if pay is None or pay.status != REFUNDED:
            return
        pay.status = PAID
        await _add_credits(s, pay.telegram_id, pay.credits, "grant", f"restore:{pay.id}")
        await s.commit()
