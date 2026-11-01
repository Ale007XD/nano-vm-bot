"""Данные пространств: списки, журнал расходов и отчёт, напоминания и их доставка."""
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.ext_db import ExtBase
from app.spaces import checklists as cl
from app.spaces import ledger as lg
from app.spaces import reminders as rm
from app.spaces import repo, service
from app.spaces.models import LedgerEntry, Reminder

OWNER, MOM, KID = 101, 102, 103


def _patch_sm(monkeypatch, maker):
    for mod in (service, repo, rm):
        monkeypatch.setattr(mod, "resolve_session_maker", lambda _=None, m=maker: m)


@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    _patch_sm(monkeypatch, maker)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    st = sys.modules["app.core.config"].settings
    for k, v in dict(SPACE_MAX_MEMBERS=10, SPACE_INVITE_TTL_DAYS=7).items():
        monkeypatch.setattr(st, k, v, raising=False)


async def fam(name="Ивановы", owner=OWNER, tz="Asia/Bangkok", members=(MOM, KID)):
    sp = await service.create_space(name, owner, kind="family", timezone_name=tz, owner_name="Папа")
    for m in members:
        await service.add_member(sp.id, m)
    return sp


async def scope_of(sp, uid):
    return await repo.open_scope(sp.id, uid)


# =============================== списки ===================================== #
class TestChecklists:
    @pytest.mark.asyncio
    async def test_default_list_is_stable(self, sm):
        s = await scope_of(await fam(), OWNER)
        a = await cl.default_list(s)
        assert (await cl.default_list(s)).id == a.id
        assert (await cl.default_list(s, "packing")).id != a.id

    @pytest.mark.asyncio
    async def test_add_normalizes_and_dedupes(self, sm):
        s = await scope_of(await fam(), OWNER)
        lst = await cl.default_list(s)
        added, skipped = await cl.add_items(s, lst.id, [("  Молоко ", "2 л"), ("молоко", ""), ("хлеб", ""), ("  ", ""), ("Хлеб", "")])
        assert [a.text for a in added] == ["Молоко", "хлеб"] and added[0].qty == "2 л"
        assert skipped == ["молоко", "Хлеб"]
        again, skipped2 = await cl.add_items(s, lst.id, [("МОЛОКО", "")])
        assert again == [] and skipped2 == ["МОЛОКО"]

    @pytest.mark.asyncio
    async def test_crossed_out_item_can_be_added_again(self, sm):
        s = await scope_of(await fam(), OWNER)
        lst = await cl.default_list(s)
        [m], _ = await cl.add_items(s, lst.id, [("молоко", "")])
        await cl.set_done(s, m.id)
        added, _ = await cl.add_items(s, lst.id, [("молоко", "")])
        assert len(added) == 1

    @pytest.mark.asyncio
    async def test_limits(self, sm, monkeypatch):
        s = await scope_of(await fam(), OWNER)
        lst = await cl.default_list(s)
        added, _ = await cl.add_items(s, lst.id, [(f"п{i}", "") for i in range(80)])
        assert len(added) == cl.MAX_BATCH
        monkeypatch.setattr(cl, "MAX_OPEN_ITEMS", 52)
        more, skipped = await cl.add_items(s, lst.id, [("а", ""), ("б", ""), ("в", "")])
        assert len(more) == 2 and skipped == ["в"]
        other = await cl.default_list(s, "x")
        [long], _ = await cl.add_items(s, other.id, [("я" * 300, "q" * 99)])
        assert len(long.text) == cl.MAX_TEXT and len(long.qty) == cl.MAX_QTY

    @pytest.mark.asyncio
    async def test_toggle_and_order_and_clear(self, sm):
        sp = await fam()
        s, k = await scope_of(sp, OWNER), await scope_of(sp, KID)
        lst = await cl.default_list(s)
        items, _ = await cl.add_items(s, lst.id, [("а", ""), ("б", ""), ("в", "")])
        assert await cl.toggle(k, items[0].id) is True  # вычеркнул другой участник
        got = await cl.list_items(s, lst.id)
        assert [i.text for i in got] == ["б", "в", "а"]
        assert got[-1].done_by == KID and got[-1].done_at is not None
        assert await cl.toggle(s, items[0].id) is False
        assert (await cl.list_items(s, lst.id))[0].done_by is None
        await cl.set_done(s, items[1].id)
        assert await cl.clear_done(s, lst.id) == 1
        assert [i.text for i in await cl.list_items(s, lst.id)] == ["а", "в"]
        assert await cl.toggle(s, 99999) is None

    @pytest.mark.asyncio
    async def test_isolation_between_spaces(self, sm):
        a, b = await fam("A", OWNER, members=()), await fam("B", 555, members=())
        sa, sb = await scope_of(a, OWNER), await scope_of(b, 555)
        la, lb = await cl.default_list(sa), await cl.default_list(sb)
        [ia], _ = await cl.add_items(sa, la.id, [("секрет A", "")])
        with pytest.raises(repo.SpaceAccessError):
            await cl.add_items(sb, la.id, [("подброс", "")])  # чужой список
        assert await cl.toggle(sb, ia.id) is None
        assert not await cl.set_done(sb, ia.id)
        assert await cl.clear_done(sb, la.id) == 0
        assert await cl.list_items(sb, la.id) == []
        assert (await cl.list_items(sa, la.id))[0].done is False
        assert await sb.count(type(lb)) == 1

    @pytest.mark.asyncio
    async def test_render_escapes(self, sm):
        s = await scope_of(await fam(), OWNER)
        lst = await cl.default_list(s)
        items, _ = await cl.add_items(s, lst.id, [("<b>х</b>", "1&2")])
        out = cl.render("Покупки <1>", items)
        assert "<b>х</b>" not in out and "&lt;b&gt;" in out and "&lt;1&gt;" in out and "1&amp;2" in out
        assert "пуст" in cl.render("П", [])


# =============================== деньги ===================================== #
class TestMoney:
    @pytest.mark.parametrize("raw,minor", [("1200", 120000), ("1 200,50", 120050), (1200.5, 120050), ("0.005", 1),
                                           ("99,999", 10000), ("1 000", 100000), (5, 500)])
    def test_to_minor(self, raw, minor):
        assert lg.to_minor(raw) == minor

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "nan", "inf", "1e30", "0.001", None])
    def test_to_minor_rejects(self, raw):
        with pytest.raises(lg.LedgerError):
            lg.to_minor(raw)

    def test_format(self):
        assert lg.format_money(120050, "RUB") == "1 200,50 ₽"
        assert lg.format_money(100, "usd") == "1 $"
        assert lg.format_money(5, "XYZ") == "0,05 XYZ"
        assert lg.format_money(-250000, "RUB") == "-2 500 ₽"


# =============================== журнал ===================================== #
async def ledger_scope(sp, uid):
    s = await scope_of(sp, uid)
    if uid == OWNER:
        await lg.ensure_categories(s, "family")
    return s


class TestLedger:
    @pytest.mark.asyncio
    async def test_categories_idempotent_and_resolution(self, sm):
        s = await scope_of(await fam(), OWNER)
        first = await lg.ensure_categories(s, "family")
        assert len(await lg.ensure_categories(s, "family")) == len(first)
        assert (await lg.resolve_category(s, "продукты")).name == "Продукты"
        assert (await lg.resolve_category(s, "Продук")).name == "Продукты"  # по началу слова
        assert (await lg.resolve_category(s, "космос")).name == "Прочее"
        assert (await lg.resolve_category(s, None)).name == "Прочее"
        assert (await lg.resolve_category(s, "ко")).name == "Прочее"  # короче 3 — не угадываем
        assert len(await s.list(type(first[0]))) == len(first)  # новые категории из текста не создаются

    @pytest.mark.asyncio
    async def test_custom_categories_from_settings(self, sm):
        sp = await service.create_space("X", OWNER, kind="company", settings={"categories": ["Аренда", " ", "Зарплата"]})
        s = await scope_of(sp, OWNER)
        names = [c.name for c in await lg.ensure_categories(s, "company", sp.settings)]
        assert names == ["Аренда", "Зарплата"]
        assert (await lg.resolve_category(s, "что-то")).name == "Прочее"  # запасная создаётся при необходимости

    @pytest.mark.asyncio
    async def test_add_entry(self, sm):
        s = await ledger_scope(await fam(), OWNER)
        e = await lg.add_entry(s, "1 200,50", currency="rub", category="продукты", note="  хлеб\n и  молоко ",
                               source="voice", raw_text="потратил тысячу двести")
        assert (e.amount_minor, e.currency, e.note, e.source, e.author_id) == (120050, "RUB", "хлеб и молоко", "voice", OWNER)
        assert e.space_id == s.space_id
        aware = datetime(2026, 11, 1, 10, 0, tzinfo=__import__("datetime").timezone(timedelta(hours=7)))
        e2 = await lg.add_entry(s, 10, currency="RUB", occurred_at=aware, source="weird")
        assert e2.occurred_at == datetime(2026, 11, 1, 3, 0) and e2.source == "text"
        with pytest.raises(lg.LedgerError):
            await lg.add_entry(s, 0, currency="RUB")

    @pytest.mark.asyncio
    async def test_undo_only_own_latest(self, sm):
        sp = await fam()
        o, m = await ledger_scope(sp, OWNER), await ledger_scope(sp, MOM)
        await lg.add_entry(o, 100, currency="RUB", note="мой")
        mine2 = await lg.add_entry(o, 200, currency="RUB", note="мой2")
        await lg.add_entry(m, 300, currency="RUB", note="мамин")
        undone = await lg.undo_last(o)
        assert undone.id == mine2.id  # а не последняя запись мамы
        assert sorted(e.note for e in await o.list(LedgerEntry)) == ["мамин", "мой"]
        assert await lg.undo_last(await scope_of(sp, KID)) is None

    @pytest.mark.asyncio
    async def test_delete_permissions_and_isolation(self, sm):
        sp, other = await fam(), await fam("Чужие", 555, members=())
        o, m, k = await ledger_scope(sp, OWNER), await ledger_scope(sp, MOM), await ledger_scope(sp, KID)
        e = await lg.add_entry(m, 100, currency="RUB")
        with pytest.raises(repo.SpaceAccessError):
            await lg.delete_entry(k, e.id)  # рядовой — чужое нельзя
        assert await lg.delete_entry(o, e.id)  # владелец — можно
        e2 = await lg.add_entry(m, 100, currency="RUB")
        assert await lg.delete_entry(m, e2.id)  # автор — своё
        e3 = await lg.add_entry(m, 100, currency="RUB")
        foreign = await scope_of(other, 555)
        assert not await lg.delete_entry(foreign, e3.id)  # из другого пространства записи не видно
        assert await o.count(LedgerEntry) == 1

    def test_month_bounds_local_time(self):
        s, e = lg.month_bounds(2026, 11, "Asia/Bangkok")
        assert (s, e) == (datetime(2026, 10, 31, 17), datetime(2026, 11, 30, 17))
        assert lg.month_bounds(2026, 12, "UTC") == (datetime(2026, 12, 1), datetime(2027, 1, 1))
        assert lg.previous_month(2026, 1) == (2025, 12)
        with pytest.raises(lg.LedgerError):
            lg.month_bounds(2026, 13, "UTC")
        with pytest.raises(lg.LedgerError):
            lg.month_bounds(2026, 1, "Mars/Base")

    @pytest.mark.asyncio
    async def test_summary(self, sm):
        sp = await fam()
        o, m = await ledger_scope(sp, OWNER), await ledger_scope(sp, MOM)
        T = lambda y, mo, d, h=12: datetime(y, mo, d, h)  # noqa: E731
        await lg.add_entry(o, 1000, currency="RUB", category="Продукты", occurred_at=T(2026, 10, 5), note="октябрь")
        await lg.add_entry(o, 3000, currency="RUB", category="Продукты", occurred_at=T(2026, 11, 3))
        await lg.add_entry(m, 500, currency="RUB", category="Транспорт", occurred_at=T(2026, 11, 4))
        await lg.add_entry(m, 7000, currency="RUB", category="Дом", occurred_at=T(2026, 11, 10), note="лампа")
        await lg.add_entry(m, 99, currency="USD", category="Дом", occurred_at=T(2026, 11, 10))  # другая валюта
        # границы по Бангкоку: 31.10 16:59 UTC = 31.10 23:59 — октябрь; 17:00 UTC = 1.11 00:00 — ноябрь
        await lg.add_entry(o, 1, currency="RUB", occurred_at=datetime(2026, 10, 31, 16, 59))
        await lg.add_entry(o, 2, currency="RUB", occurred_at=datetime(2026, 10, 31, 17, 0))
        s = await lg.monthly_summary(o, 2026, 11, tz="Asia/Bangkok", currency="RUB")
        assert s.total == (3000 + 500 + 7000 + 2) * 100 and s.count == 4
        assert s.prev_total == (1000 + 1) * 100
        assert s.by_category[0] == ("Дом", 700000) and s.by_category[1] == ("Продукты", 300000)
        assert dict(s.by_author) == {OWNER: 300200, MOM: 750000} and s.by_author[0][0] == MOM
        assert [e.note for e in s.biggest][0] == "лампа" and len(s.biggest) == 3
        assert s.days == 30

    @pytest.mark.asyncio
    async def test_summary_isolated_between_spaces(self, sm):
        a, b = await fam("A", OWNER, members=()), await fam("B", 555, members=())
        sa = await ledger_scope(a, OWNER)
        sb = await scope_of(b, 555)
        await lg.ensure_categories(sb, "family")
        await lg.add_entry(sa, 5000, currency="RUB", occurred_at=datetime(2026, 11, 3))
        s = await lg.monthly_summary(sb, 2026, 11, tz="UTC", currency="RUB")
        assert (s.total, s.count, s.by_category) == (0, 0, [])

    @pytest.mark.asyncio
    async def test_report_text(self, sm):
        sp = await fam()
        o, m = await ledger_scope(sp, OWNER), await ledger_scope(sp, MOM)
        await lg.add_entry(o, 1000, currency="RUB", category="Продукты", occurred_at=datetime(2026, 10, 5))
        await lg.add_entry(o, 3000, currency="RUB", category="Продукты", occurred_at=datetime(2026, 11, 5), note="<i>x</i>")
        await lg.add_entry(m, 1500, currency="RUB", category="Дом", occurred_at=datetime(2026, 11, 6))
        s = await lg.monthly_summary(o, 2026, 11, tz="UTC", currency="RUB")
        text = lg.format_report(s, {OWNER: "Папа <b>", MOM: "Мама"}, "Расходы")
        assert "ноябрь 2026" in text and "4 500 ₽" in text and "▲" in text and "+350%" in text
        assert "Продукты — 3 000 ₽ (67%)" in text
        assert "<i>x</i>" not in text and "Папа &lt;b&gt;" in text
        empty = lg.format_report(await lg.monthly_summary(o, 2020, 1, tz="UTC", currency="RUB"), {})
        assert "расходов нет" in empty


# ============================== напоминания ================================= #
NOW = datetime(2026, 11, 1, 12, 0)


class Outbox:
    def __init__(self, fail=()):
        self.sent, self.fail = [], set(fail)

    async def __call__(self, chat_id, text):
        if chat_id in self.fail:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


class TestReminderCreate:
    @pytest.mark.asyncio
    async def test_validation(self, sm):
        sp = await fam()
        s = await scope_of(sp, OWNER)
        kw = dict(tz="Asia/Bangkok", now=NOW)
        with pytest.raises(rm.ReminderError):
            await rm.create(s, "  ", run_at=NOW + timedelta(hours=1), **kw)
        with pytest.raises(rm.ReminderError):
            await rm.create(s, "x", **kw)  # ни времени, ни расписания
        with pytest.raises(rm.ReminderError):
            await rm.create(s, "x", run_at=NOW + timedelta(hours=1), cron="0 9 * * *", **kw)
        with pytest.raises(rm.ReminderError):
            await rm.create(s, "x", run_at=NOW - timedelta(hours=1), **kw)
        with pytest.raises(rm.ReminderError):
            await rm.create(s, "x", run_at=NOW + timedelta(hours=1), tz="Mars/Base", now=NOW)
        with pytest.raises(rm.ReminderError):
            await rm.create(s, "x", cron="не cron", **kw)
        with pytest.raises(rm.ReminderError):
            await rm.create(s, "x", run_at=NOW + timedelta(hours=1), assignee_id=999, **kw)  # не участник
        assert await s.count(Reminder) == 0

    @pytest.mark.asyncio
    async def test_create_normalizes(self, sm):
        s = await scope_of(await fam(), OWNER)
        aware = datetime(2026, 11, 2, 9, 0, tzinfo=__import__("datetime").timezone(timedelta(hours=7)))
        r = await rm.create(s, " купить\n хлеб ", run_at=aware, tz="Asia/Bangkok", assignee_id=MOM, now=NOW)
        assert (r.text, r.next_run_at, r.assignee_id, r.created_by) == ("купить хлеб", datetime(2026, 11, 2, 2, 0), MOM, OWNER)
        c = await rm.create(s, "зарядка", cron="0 9 * * *", tz="Asia/Bangkok", now=NOW)
        assert c.next_run_at == datetime(2026, 11, 2, 2, 0)  # ближайшие 09:00 по Бангкоку

    @pytest.mark.asyncio
    async def test_active_limit(self, sm, monkeypatch):
        s = await scope_of(await fam(), OWNER)
        monkeypatch.setattr(rm, "MAX_ACTIVE", 2)
        for i in range(2):
            await rm.create(s, f"r{i}", run_at=NOW + timedelta(hours=1), tz="UTC", now=NOW)
        with pytest.raises(rm.ReminderError):
            await rm.create(s, "r3", run_at=NOW + timedelta(hours=1), tz="UTC", now=NOW)

    @pytest.mark.asyncio
    async def test_cancel_rules(self, sm):
        sp = await fam()
        o, m, k = await scope_of(sp, OWNER), await scope_of(sp, MOM), await scope_of(sp, KID)
        r = await rm.create(m, "мамино", run_at=NOW + timedelta(hours=1), tz="UTC", now=NOW)
        with pytest.raises(repo.SpaceAccessError):
            await rm.cancel(k, r.id)
        assert await rm.cancel(m, r.id) is True
        assert await rm.cancel(m, r.id) is False  # уже отменено
        r2 = await rm.create(m, "ещё", run_at=NOW + timedelta(hours=1), tz="UTC", now=NOW)
        assert await rm.cancel(o, r2.id) is True  # владелец может любое
        assert await rm.list_active(o) == []


class TestReminderDelivery:
    @pytest.mark.asyncio
    async def test_broadcast_once_then_done(self, sm):
        s = await scope_of(await fam(), OWNER)
        r = await rm.create(s, "ужин <в 19:00>", run_at=NOW + timedelta(minutes=5), tz="UTC", now=NOW)
        out = Outbox()
        assert await rm.run_once(out, NOW + timedelta(minutes=1)) == 0 and out.sent == []  # рано
        assert await rm.run_once(out, NOW + timedelta(minutes=6)) == 1
        assert sorted(c for c, _ in out.sent) == [OWNER, MOM, KID]
        assert "&lt;в 19:00&gt;" in out.sent[0][1] and "ужин <" not in out.sent[0][1]
        assert await rm.run_once(out, NOW + timedelta(minutes=7)) == 0 and len(out.sent) == 3  # повтора нет
        got = await s.get(Reminder, r.id)
        assert got.is_active is False and got.run_count == 1 and got.leased_until is None

    @pytest.mark.asyncio
    async def test_assignee_only_and_fallbacks(self, sm):
        sp = await fam()
        s = await scope_of(sp, OWNER)
        await rm.create(s, "маме", run_at=NOW + timedelta(minutes=1), tz="UTC", assignee_id=MOM, now=NOW)
        out = Outbox()
        await rm.run_once(out, NOW + timedelta(minutes=2))
        assert [c for c, _ in out.sent] == [MOM]
        # адресат вышел из пространства — напоминание получает автор
        await rm.create(s, "сыну", run_at=NOW + timedelta(minutes=10), tz="UTC", assignee_id=KID, now=NOW)
        await service.remove_member(sp.id, KID)
        out2 = Outbox()
        await rm.run_once(out2, NOW + timedelta(minutes=11))
        assert [c for c, _ in out2.sent] == [OWNER]

    @pytest.mark.asyncio
    async def test_spaces_do_not_cross(self, sm):
        a, b = await fam("A", OWNER, members=(MOM,)), await fam("B", 555, members=(556,))
        await rm.create(await scope_of(a, OWNER), "для A", run_at=NOW + timedelta(minutes=1), tz="UTC", now=NOW)
        await rm.create(await scope_of(b, 555), "для B", run_at=NOW + timedelta(minutes=1), tz="UTC", now=NOW)
        out = Outbox()
        await rm.run_once(out, NOW + timedelta(minutes=2))
        by_chat = {c: txt for c, txt in out.sent}
        assert sorted(by_chat) == [OWNER, MOM, 555, 556]
        assert "для A" in by_chat[OWNER] and "для A" in by_chat[MOM]
        assert "для B" in by_chat[555] and "для B" in by_chat[556]

    @pytest.mark.asyncio
    async def test_inactive_space_is_silent_and_keeps_reminder(self, sm):
        sp = await fam()
        s = await scope_of(sp, OWNER)
        r = await rm.create(s, "x", run_at=NOW + timedelta(minutes=1), tz="UTC", now=NOW)
        await service.deactivate_space(sp.id)
        out = Outbox()
        assert await rm.run_once(out, NOW + timedelta(minutes=2)) == 0 and out.sent == []
        async with sm() as ses:
            assert (await ses.get(Reminder, r.id)).is_active is True

    @pytest.mark.asyncio
    async def test_recurring_reschedules(self, sm):
        s = await scope_of(await fam(members=()), OWNER)
        r = await rm.create(s, "таблетка", cron="0 9 * * *", tz="Asia/Bangkok", now=NOW)  # 2.11 02:00 UTC
        out = Outbox()
        late = datetime(2026, 11, 2, 2, 0, 30)
        await rm.run_once(out, late)
        got = await s.get(Reminder, r.id)
        assert got.is_active and got.run_count == 1 and got.next_run_at == datetime(2026, 11, 3, 2, 0)
        # долгий простой: пропущенные дни не «досылаются» пачкой — одно сообщение и следующий будущий слот
        await rm.run_once(out, datetime(2026, 11, 10, 5, 0))
        got = await s.get(Reminder, r.id)
        assert len(out.sent) == 2 and got.next_run_at == datetime(2026, 11, 11, 2, 0)

    @pytest.mark.asyncio
    async def test_partial_failure_still_completes(self, sm):
        s = await scope_of(await fam(), OWNER)
        await rm.create(s, "всем", run_at=NOW + timedelta(minutes=1), tz="UTC", now=NOW)
        out = Outbox(fail={MOM})
        assert await rm.run_once(out, NOW + timedelta(minutes=2)) == 1
        assert sorted(c for c, _ in out.sent) == [OWNER, KID]
        assert await rm.list_active(s) == []

    @pytest.mark.asyncio
    async def test_total_failure_retries_after_lease_and_gives_up(self, sm, caplog):
        s = await scope_of(await fam(members=()), OWNER)
        r = await rm.create(s, "важно", run_at=NOW + timedelta(minutes=1), tz="UTC", now=NOW)
        bad = Outbox(fail={OWNER})
        t = NOW + timedelta(minutes=2)
        await rm.run_once(bad, t)
        assert (await s.get(Reminder, r.id)).attempts == 1
        await rm.run_once(bad, t + timedelta(seconds=10))  # аренда ещё действует — не пробуем
        assert (await s.get(Reminder, r.id)).attempts == 1
        good = Outbox()
        await rm.run_once(good, t + timedelta(seconds=rm.LEASE_SECONDS + 1))
        assert len(good.sent) == 1 and (await s.get(Reminder, r.id)).is_active is False
        # до упора
        r2 = await rm.create(s, "ещё", run_at=NOW + timedelta(minutes=1), tz="UTC", now=NOW)
        with caplog.at_level(logging.ERROR):
            for i in range(rm.MAX_ATTEMPTS):
                await rm.run_once(bad, t + timedelta(seconds=(rm.LEASE_SECONDS + 1) * (i + 5)))
        got = await s.get(Reminder, r2.id)
        assert got.is_active is False and got.attempts == rm.MAX_ATTEMPTS and "deactivated" in caplog.text

    @pytest.mark.asyncio
    async def test_author_left_assignee_still_gets_it(self, sm):
        sp = await fam(members=(MOM,))
        s = await scope_of(sp, OWNER)
        await rm.create(s, "маме", run_at=NOW + timedelta(minutes=1), tz="UTC", assignee_id=MOM, now=NOW)
        await service.set_role(sp.id, MOM, "owner")
        await service.remove_member(sp.id, OWNER)  # автор вышел
        out = Outbox()
        await rm.run_once(out, NOW + timedelta(minutes=2))
        assert [c for c, _ in out.sent] == [MOM]

    @pytest.mark.asyncio
    async def test_everyone_gone_closes_reminder(self, sm):
        sp = await fam(members=(MOM,))
        s = await scope_of(sp, OWNER)
        r = await rm.create(s, "x", run_at=NOW + timedelta(minutes=1), tz="UTC", assignee_id=MOM, now=NOW)
        async with sm() as ses:  # адресат и автор ушли (обход защиты владельца — прямой правкой таблицы)
            from sqlalchemy import delete

            from app.spaces.models import SpaceMember
            await ses.execute(delete(SpaceMember).where(SpaceMember.space_id == sp.id, SpaceMember.user_id.in_([OWNER, MOM])))
            await ses.commit()
        out = Outbox()
        await rm.run_once(out, NOW + timedelta(minutes=2))
        assert out.sent == []
        async with sm() as ses:
            assert (await ses.get(Reminder, r.id)).is_active is False

    @pytest.mark.asyncio
    async def test_parallel_workers_deliver_once(self, tmp_path, monkeypatch):
        # файловая БД: у каждой сессии своё соединение (в :memory: оно общее)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(ExtBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        _patch_sm(monkeypatch, maker)
        s = await scope_of(await fam(members=()), OWNER)
        await rm.create(s, "один раз", run_at=NOW + timedelta(minutes=1), tz="UTC", now=NOW)
        out = Outbox()
        await asyncio.gather(*(rm.run_once(out, NOW + timedelta(minutes=2)) for _ in range(4)))
        await engine.dispose()
        assert len(out.sent) == 1

    @pytest.mark.asyncio
    async def test_run_once_never_raises(self, sm, monkeypatch):
        monkeypatch.setattr(rm, "_claim_due", AsyncMock(side_effect=RuntimeError("db down")))
        assert await rm.run_once(Outbox()) == 0

    @pytest.mark.asyncio
    async def test_loop_stops_on_event(self, sm):
        s = await scope_of(await fam(members=()), OWNER)
        now = rm._now()
        await rm.create(s, "x", run_at=now - timedelta(seconds=30), tz="UTC", now=now - timedelta(seconds=40))
        out, stop = Outbox(), asyncio.Event()
        task = asyncio.create_task(rm.run_loop(out, 0.05, stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 2)
        assert len(out.sent) == 1


class TestRunner:
    @pytest.mark.asyncio
    async def test_start_stop(self, monkeypatch):
        from app.spaces import runner

        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(rm, "run_once", AsyncMock(return_value=0))
        monkeypatch.setattr(st, "SPACES_ENABLED", False, raising=False)
        assert runner.start(MagicMock()) is None
        monkeypatch.setattr(st, "SPACES_ENABLED", True, raising=False)
        monkeypatch.setattr(st, "SPACE_REMINDER_POLL_SECONDS", 1, raising=False)
        bot = MagicMock()
        t1 = runner.start(bot)
        assert t1 is runner.start(bot) and not t1.done()
        await asyncio.sleep(0.05)
        await runner.stop()
        assert t1.done() and runner._task is None
        await runner.stop()  # повторная остановка безопасна

    def test_poll_seconds_ignores_junk(self, monkeypatch):
        from app.spaces import runner

        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "SPACE_REMINDER_POLL_SECONDS", "x", raising=False)
        assert runner.poll_seconds() == 15.0
        monkeypatch.setattr(st, "SPACE_REMINDER_POLL_SECONDS", 30, raising=False)
        assert runner.poll_seconds() == 30.0

    @pytest.mark.asyncio
    async def test_send_uses_html(self, monkeypatch):
        from app.spaces import runner

        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "SPACES_ENABLED", True, raising=False)
        captured = {}

        async def fake_loop(send, interval, stop):
            captured["send"] = send

        monkeypatch.setattr(rm, "run_loop", fake_loop)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        t = runner.start(bot)
        await t
        await captured["send"](5, "привет")
        bot.send_message.assert_awaited_once_with(chat_id=5, text="привет", parse_mode="HTML")
        runner._task = None


class TestRegistered:
    def test_new_tables_registered(self):
        names = set(ExtBase.metadata.tables)
        assert {"checklists", "checklist_items", "categories", "ledger_entries", "space_reminders"} <= names

    def test_all_data_models_are_space_owned(self):
        from app.spaces import models

        for name in ("Checklist", "ChecklistItem", "Category", "LedgerEntry", "Reminder"):
            assert hasattr(getattr(models, name), "space_id"), name
