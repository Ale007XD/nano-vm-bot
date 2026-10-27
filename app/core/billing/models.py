"""Таблицы биллинга (на ExtBase, создаются init_ext_tables)."""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)

from app.core.ext_db import ExtBase

PAID = "paid"
REFUNDED = "refunded"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CreditAccount(ExtBase):
    """Баланс кредитов. 1 кредит = 1 сообщение сверх бесплатного суточного лимита."""

    __tablename__ = "credit_accounts"

    telegram_id = Column(BigInteger, primary_key=True)
    balance = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class CreditLedger(ExtBase):
    """Журнал движения кредитов (аудит; баланс — в CreditAccount)."""

    __tablename__ = "credit_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    delta = Column(Integer, nullable=False)
    reason = Column(String(16), nullable=False)  # purchase / spend / grant / refund
    ref = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, default=_now)


class Payment(ExtBase):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("provider", "charge_id", name="uq_payment_charge"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(16), nullable=False)
    charge_id = Column(String(128), nullable=False)  # telegram_payment_charge_id
    telegram_id = Column(BigInteger, nullable=False, index=True)
    package_id = Column(String(32), nullable=False)
    credits = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)  # в минимальных единицах валюты
    currency = Column(String(8), nullable=False)
    status = Column(String(8), nullable=False, default=PAID)
    created_at = Column(DateTime, default=_now)
