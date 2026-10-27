"""
Биллинг: пакеты, кредиты, идемпотентность платежей, провайдеры, хендлеры, лимит+кредиты.
БД — in-memory SQLite; Telegram — fake-бот.
"""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import ext_db
from app.core.billing import models, packages, providers, service
from app.core.ext_db import ExtBase

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
from app.handlers import billing as h  # noqa: E402

U1, U2, ADMIN = 111, 222, 999
PKG = packages.Package("s50", 50, "50 запросов", {"XTR": 100, "RUB": 19900})


@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ext_db, "default_session_maker", lambda: maker)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "ADMIN_IDS", [ADMIN])
    monkeypatch.setattr(st, "BILLING_PROVIDERS", "stars")
    monkeypatch.setattr(st, "YOOKASSA_PROVIDER_TOKEN", "")
    monkeypatch.setattr(st, "STRIPE_PROVIDER_TOKEN", "")
    monkeypatch.setattr(st, "CREDIT_PACKAGES", "")
    monkeypatch.setattr(h, "user_allowed", AsyncMock(return_value=True))


# ------------------------------- пакеты ----------------------------------- #
class TestPackages:
    def test_defaults_valid(self):
        pk = packages.parse_packages(packages.DEFAULT_PACKAGES_JSON)
        assert [p.id for p in pk] == ["s50", "s250"]
        assert pk[0].price("XTR") == 100 and pk[0].price("EUR") is None

    def test_bad_json_falls_back_to_defaults(self):
        assert packages.parse_packages("{not json")
        assert packages.parse_packages('{"a": 1}')  # не список

    @pytest.mark.parametrize("bad", [
        {"id": "a:b", "credits": 5, "prices": {"XTR": 1}},     # ':' ломает payload
        {"id": "x", "credits": 0, "prices": {"XTR": 1}},
        {"id": "x", "credits": 5, "prices": {"XTR": 0}},
        {"id": "x", "credits": 5, "prices": {}},
        {"id": "x", "credits": "много", "prices": {"XTR": 1}},
        {"credits": 5, "prices": {"XTR": 1}},
        {"id": "y" * 40, "credits": 5, "prices": {"XTR": 1}},
    ])
    def test_bad_entries_skipped(self, bad):
        good = {"id": "ok", "credits": 5, "prices": {"xtr": 7}}
        out = packages.parse_packages(json.dumps([bad, good]))
        assert [p.id for p in out] == ["ok"] and out[0].price("XTR") == 7

    def test_duplicate_ids_skipped(self):
        e = {"id": "a", "credits": 1, "prices": {"XTR": 1}}
        assert len(packages.parse_packages(json.dumps([e, e]))) == 1


# ------------------------------ кредиты ----------------------------------- #
class TestCredits:
    @pytest.mark.asyncio
    async def test_purchase_credits_balance(self, sm):
        pay = await service.record_payment(U1, PKG, "stars", "ch1", 100, "XTR")
        assert pay.id and await service.get_balance(U1) == 50
        assert await service.get_balance(U2) == 0

    @pytest.mark.asyncio
    async def test_duplicate_delivery_is_idempotent(self, sm):
        assert await service.record_payment(U1, PKG, "stars", "ch1", 100, "XTR")
        assert await service.record_payment(U1, PKG, "stars", "ch1", 100, "XTR") is None
        assert await service.get_balance(U1) == 50

    @pytest.mark.asyncio
    async def test_same_charge_id_different_provider_is_distinct(self, sm):
        await service.record_payment(U1, PKG, "stars", "x", 100, "XTR")
        assert await service.record_payment(U1, PKG, "yookassa", "x", 19900, "RUB")
        assert await service.get_balance(U1) == 100

    @pytest.mark.asyncio
    async def test_consume(self, sm):
        await service.grant_credits(U1, 2)
        assert await service.consume_credit(U1)
        assert await service.consume_credit(U1)
        assert not await service.consume_credit(U1)
        assert await service.get_balance(U1) == 0

    @pytest.mark.asyncio
    async def test_consume_without_account(self, sm):
        assert not await service.consume_credit(U1)
        assert await service.get_balance(U1) == 0

    @pytest.mark.asyncio
    async def test_parallel_consume_never_overspends(self, tmp_path):
        # файловая БД: у каждой сессии своё соединение (in-memory делит одно на всех)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'b.db'}", connect_args={"timeout": 30})
        async with engine.begin() as conn:
            await conn.run_sync(ExtBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await service.grant_credits(U1, 5, session_maker=maker)
            res = await asyncio.gather(*[service.consume_credit(U1, session_maker=maker) for _ in range(12)])
            assert sum(res) == 5 and await service.get_balance(U1, maker) == 0
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_grant_validates_and_accumulates(self, sm):
        with pytest.raises(ValueError):
            await service.grant_credits(U1, 0)
        with pytest.raises(ValueError):
            await service.grant_credits(U1, -5)
        await service.grant_credits(U1, 3)
        assert await service.grant_credits(U1, 4) == 7

    @pytest.mark.asyncio
    async def test_ledger_records_movements(self, sm):
        await service.record_payment(U1, PKG, "stars", "c", 100, "XTR")
        await service.consume_credit(U1)
        from sqlalchemy import select

        async with sm() as s:
            rows = (await s.execute(select(models.CreditLedger).order_by(models.CreditLedger.id))).scalars().all()
        assert [(r.delta, r.reason) for r in rows] == [(50, "purchase"), (-1, "spend")]


class TestReverse:
    @pytest.mark.asyncio
    async def test_reverse_ok(self, sm):
        pay = await service.record_payment(U1, PKG, "stars", "c", 100, "XTR")
        assert await service.reverse_payment(pay.id) == "ok"
        assert await service.get_balance(U1) == 0
        assert (await service.get_payment(pay.id)).status == models.REFUNDED
        assert await service.reverse_payment(pay.id) == "already_refunded"
        assert await service.get_balance(U1) == 0

    @pytest.mark.asyncio
    async def test_reverse_refused_when_spent(self, sm):
        pay = await service.record_payment(U1, PKG, "stars", "c", 100, "XTR")
        await service.consume_credit(U1)
        assert await service.reverse_payment(pay.id) == "credits_spent"
        assert await service.get_balance(U1) == 49
        assert (await service.get_payment(pay.id)).status == models.PAID

    @pytest.mark.asyncio
    async def test_reverse_not_found(self, sm):
        assert await service.reverse_payment(12345) == "not_found"

    @pytest.mark.asyncio
    async def test_restore_after_failed_refund(self, sm):
        pay = await service.record_payment(U1, PKG, "stars", "c", 100, "XTR")
        await service.reverse_payment(pay.id)
        await service.restore_payment(pay.id)
        assert await service.get_balance(U1) == 50
        assert (await service.get_payment(pay.id)).status == models.PAID
        await service.restore_payment(pay.id)  # повторно — без эффекта
        assert await service.get_balance(U1) == 50


# ----------------------------- провайдеры --------------------------------- #
class TestProviders:
    def test_payload_roundtrip_and_garbage(self):
        p = providers.PaymentProvider.make_payload("stars", "s50", 7)
        assert providers.PaymentProvider.parse_payload(p) == ("stars", "s50", 7)
        for bad in ("", "x", "buy:stars:s50", "buy:stars:s50:abc", "pay:a:b:1", None):
            assert providers.PaymentProvider.parse_payload(bad) is None

    def test_disabled_by_default(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "BILLING_PROVIDERS", "")
        assert providers.build_providers(st) == {} and not providers.billing_enabled(st)

    def test_fiat_needs_token(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "BILLING_PROVIDERS", "stars, yookassa,stripe,bogus")
        assert set(providers.build_providers(st)) == {"stars"}
        monkeypatch.setattr(st, "YOOKASSA_PROVIDER_TOKEN", "381764678:TEST:1")
        got = providers.build_providers(st)
        assert set(got) == {"stars", "yookassa"} and got["yookassa"].currency == "RUB"

    @pytest.mark.asyncio
    async def test_stars_invoice_has_empty_token_and_xtr(self):
        bot = MagicMock(send_invoice=AsyncMock())
        await providers.StarsProvider().send_invoice(bot, 5, U1, PKG)
        kw = bot.send_invoice.await_args.kwargs
        assert kw["currency"] == "XTR" and kw["provider_token"] == ""
        assert kw["prices"][0].amount == 100
        assert kw["payload"] == "buy:stars:s50:111"
        assert len(kw["title"]) <= 32 and len(kw["description"]) <= 255

    @pytest.mark.asyncio
    async def test_fiat_invoice_uses_token_and_price(self):
        prov = providers.TelegramInvoiceProvider("yookassa", "Y", "RUB", "tok")
        bot = MagicMock(send_invoice=AsyncMock())
        await prov.send_invoice(bot, 5, U1, PKG)
        kw = bot.send_invoice.await_args.kwargs
        assert kw["provider_token"] == "tok" and kw["prices"][0].amount == 19900

    @pytest.mark.asyncio
    async def test_invoice_requires_price_in_currency(self):
        prov = providers.TelegramInvoiceProvider("stripe", "S", "USD", "t")
        with pytest.raises(ValueError):
            await prov.send_invoice(MagicMock(send_invoice=AsyncMock()), 5, U1, PKG)

    @pytest.mark.asyncio
    async def test_refunds(self):
        bot = MagicMock(refund_star_payment=AsyncMock(return_value=True))
        assert await providers.StarsProvider().refund(bot, U1, "ch") is True
        bot.refund_star_payment.assert_awaited_once_with(user_id=U1, telegram_payment_charge_id="ch")
        assert await providers.TelegramInvoiceProvider("y", "Y", "RUB", "t").refund(bot, U1, "c") is False


# ------------------------------ хендлеры ---------------------------------- #
def user(uid=U1):
    return SimpleNamespace(id=uid, first_name="И", last_name=None, username=None, is_bot=False)


def msg(uid=U1, **kw):
    m = SimpleNamespace(
        from_user=user(uid), chat=SimpleNamespace(id=uid, type="private"),
        bot=MagicMock(refund_star_payment=AsyncMock(return_value=True)),
        answer=AsyncMock(), successful_payment=None,
    )
    m.__dict__.update(kw)
    return m


def precheckout(payload, amount=100, currency="XTR", uid=U1):
    return SimpleNamespace(
        invoice_payload=payload, total_amount=amount, currency=currency,
        from_user=user(uid), answer=AsyncMock(),
    )


def success(payload, charge="chg1", amount=100, currency="XTR", uid=U1):
    sp = SimpleNamespace(
        invoice_payload=payload, telegram_payment_charge_id=charge,
        total_amount=amount, currency=currency,
    )
    return msg(uid, successful_payment=sp)


class TestHandlers:
    @pytest.mark.asyncio
    async def test_buy_lists_only_priced_offers(self):
        m = msg()
        await h.cmd_buy(m)
        kb = m.answer.await_args.kwargs["reply_markup"]
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert datas == ["buy:stars:s50", "buy:stars:s250"]

    @pytest.mark.asyncio
    async def test_buy_disabled(self, monkeypatch):
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "BILLING_PROVIDERS", "")
        m = msg()
        await h.cmd_buy(m)
        assert "недоступна" in m.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_callback_sends_invoice(self):
        inner = msg()
        cb = SimpleNamespace(data="buy:stars:s50", from_user=user(), message=inner,
                             bot=MagicMock(send_invoice=AsyncMock()), answer=AsyncMock())
        await h.cb_buy(cb)
        cb.bot.send_invoice.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", ["buy:stars:nope", "buy:stripe:s50", "buy:stars", "buy:a:b:c"])
    async def test_callback_rejects_unknown(self, data):
        inner = msg()
        cb = SimpleNamespace(data=data, from_user=user(), message=inner,
                             bot=MagicMock(send_invoice=AsyncMock()), answer=AsyncMock())
        await h.cb_buy(cb)
        cb.bot.send_invoice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_precheckout_ok(self):
        q = precheckout("buy:stars:s50:111")
        await h.pre_checkout(q)
        q.answer.assert_awaited_once_with(ok=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kw", [
        dict(payload="garbage"),
        dict(payload="buy:stars:s50:222"),                 # чужой счёт
        dict(payload="buy:stars:ghost:111"),               # нет пакета
        dict(payload="buy:stripe:s50:111"),                # провайдер выключен
        dict(payload="buy:stars:s50:111", amount=1),       # подмена цены
        dict(payload="buy:stars:s50:111", currency="RUB"), # подмена валюты
    ])
    async def test_precheckout_rejects(self, kw):
        q = precheckout(**kw)
        await h.pre_checkout(q)
        assert q.answer.await_args.kwargs["ok"] is False and q.answer.await_args.kwargs["error_message"]

    @pytest.mark.asyncio
    async def test_paid_credits_and_replays_safely(self, sm):
        m = success("buy:stars:s50:111")
        await h.on_paid(m)
        assert await service.get_balance(U1) == 50
        assert "Оплата получена" in m.answer.await_args.args[0]
        again = success("buy:stars:s50:111")
        await h.on_paid(again)
        assert await service.get_balance(U1) == 50
        again.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_paid_unmatched_is_reported_not_credited(self, sm):
        m = success("buy:stars:ghost:111")
        await h.on_paid(m)
        assert await service.get_balance(U1) == 0
        assert "paysupport" in m.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_paid_for_someone_elses_invoice_not_credited(self, sm):
        await h.on_paid(success("buy:stars:s50:222", uid=U1))
        assert await service.get_balance(U1) == 0 and await service.get_balance(U2) == 0

    @pytest.mark.asyncio
    async def test_balance_view(self, sm):
        await h.on_paid(success("buy:stars:s50:111"))
        m = msg()
        await h.cmd_balance(m)
        text = m.answer.await_args.args[0]
        assert "50" in text and "s50" in text

    @pytest.mark.asyncio
    async def test_grant_admin_only(self, sm):
        from aiogram.filters import CommandObject

        cmd = CommandObject(prefix="/", command="grant", args="222 10")
        user_m = msg(U1)
        await h.cmd_grant(user_m, cmd)
        assert await service.get_balance(U2) == 0
        await h.cmd_grant(msg(ADMIN), cmd)
        assert await service.get_balance(U2) == 10

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["", "222", "abc 5", "222 0", "222 -5", "222 x"])
    async def test_grant_bad_args(self, sm, args):
        from aiogram.filters import CommandObject

        m = msg(ADMIN)
        await h.cmd_grant(m, CommandObject(prefix="/", command="grant", args=args))
        assert await service.get_balance(U2) == 0 and "Формат" in m.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_refund_flow_stars(self, sm):
        from aiogram.filters import CommandObject

        await h.on_paid(success("buy:stars:s50:111", charge="chgX"))
        pid = (await service.recent_payments(U1))[0].id
        m = msg(ADMIN)
        await h.cmd_refund(m, CommandObject(prefix="/", command="refund", args=str(pid)))
        m.bot.refund_star_payment.assert_awaited_once_with(user_id=U1, telegram_payment_charge_id="chgX")
        assert await service.get_balance(U1) == 0

    @pytest.mark.asyncio
    async def test_refund_non_admin_ignored(self, sm):
        from aiogram.filters import CommandObject

        await h.on_paid(success("buy:stars:s50:111"))
        pid = (await service.recent_payments(U1))[0].id
        m = msg(U1)
        await h.cmd_refund(m, CommandObject(prefix="/", command="refund", args=str(pid)))
        m.bot.refund_star_payment.assert_not_awaited()
        assert await service.get_balance(U1) == 50

    @pytest.mark.asyncio
    async def test_refund_blocked_when_credits_spent(self, sm):
        from aiogram.filters import CommandObject

        await h.on_paid(success("buy:stars:s50:111"))
        await service.consume_credit(U1)
        pid = (await service.recent_payments(U1))[0].id
        m = msg(ADMIN)
        await h.cmd_refund(m, CommandObject(prefix="/", command="refund", args=str(pid)))
        m.bot.refund_star_payment.assert_not_awaited()
        assert await service.get_balance(U1) == 49

    @pytest.mark.asyncio
    async def test_refund_provider_failure_restores_credits(self, sm):
        from aiogram.filters import CommandObject

        await h.on_paid(success("buy:stars:s50:111"))
        pid = (await service.recent_payments(U1))[0].id
        m = msg(ADMIN)
        m.bot.refund_star_payment.side_effect = RuntimeError("api down")
        await h.cmd_refund(m, CommandObject(prefix="/", command="refund", args=str(pid)))
        assert await service.get_balance(U1) == 50
        assert (await service.get_payment(pid)).status == models.PAID


# ------------------- лимит + кредиты (интеграция с access) ---------------- #
class TestLimitIntegration:
    @pytest.fixture
    def over_limit(self, monkeypatch, sm):
        from app.core import byok

        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "USER_DAILY_MESSAGE_LIMIT", 5)
        db = sys.modules["app.core.database"]
        monkeypatch.setattr(db, "get_bot_setting", AsyncMock(return_value=None), raising=False)
        monkeypatch.setattr(db, "count_today_messages", AsyncMock(return_value=50), raising=False)
        monkeypatch.setattr(db, "get_today_usage", AsyncMock(return_value=None), raising=False)
        monkeypatch.setattr(byok, "has_user_keys", AsyncMock(return_value=False))

    @pytest.mark.asyncio
    async def test_denied_without_credits_mentions_buy(self, over_limit):
        from app.core.access import check_daily_limit

        res = await check_daily_limit(U1)
        assert res.allowed is False and "/buy" in res.message

    @pytest.mark.asyncio
    async def test_no_buy_hint_when_billing_off(self, over_limit, monkeypatch):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(sys.modules["app.core.config"].settings, "BILLING_PROVIDERS", "")
        res = await check_daily_limit(U1)
        assert res.allowed is False and "/buy" not in res.message

    @pytest.mark.asyncio
    async def test_credit_spent_one_per_message(self, over_limit):
        from app.core.access import check_daily_limit

        await service.grant_credits(U1, 2)
        assert (await check_daily_limit(U1)).allowed is True
        assert await service.get_balance(U1) == 1
        assert (await check_daily_limit(U1)).allowed is True
        assert (await check_daily_limit(U1)).allowed is False
        assert await service.get_balance(U1) == 0

    @pytest.mark.asyncio
    async def test_credits_not_spent_under_limit(self, over_limit, monkeypatch):
        from app.core.access import check_daily_limit

        db = sys.modules["app.core.database"]
        monkeypatch.setattr(db, "count_today_messages", AsyncMock(return_value=1), raising=False)
        await service.grant_credits(U1, 3)
        assert (await check_daily_limit(U1)).allowed is True
        assert await service.get_balance(U1) == 3

    @pytest.mark.asyncio
    async def test_admin_and_byok_never_charged(self, over_limit, monkeypatch):
        from app.core import byok
        from app.core.access import check_daily_limit

        await service.grant_credits(ADMIN, 3)
        await service.grant_credits(U1, 3)
        assert (await check_daily_limit(ADMIN)).allowed is True
        monkeypatch.setattr(byok, "has_user_keys", AsyncMock(return_value=True))
        assert (await check_daily_limit(U1)).allowed is True
        assert await service.get_balance(ADMIN) == 3 and await service.get_balance(U1) == 3

    @pytest.mark.asyncio
    async def test_credits_are_per_user(self, over_limit):
        from app.core.access import check_daily_limit

        await service.grant_credits(U2, 5)
        assert (await check_daily_limit(U1)).allowed is False
        assert await service.get_balance(U2) == 5

    @pytest.mark.asyncio
    async def test_billing_db_failure_denies_without_crash(self, over_limit, monkeypatch):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(service, "consume_credit", AsyncMock(side_effect=RuntimeError("db")))
        assert (await check_daily_limit(U1)).allowed is False


class TestWiring:
    def test_router_registered_before_messages(self):
        from pathlib import Path

        src = Path("app/handlers/__init__.py").read_text(encoding="utf-8")
        assert src.index("include_router(billing.router)") < src.index("include_router(messages.router)")

    def test_ext_models_registered(self):
        src = Path_read("app/core/ext_db.py")
        assert "app.core.billing.models" in src


def Path_read(p):
    from pathlib import Path

    return Path(p).read_text(encoding="utf-8")
