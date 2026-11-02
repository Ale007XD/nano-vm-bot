"""1.24.0: запись своих показателей через разбор сообщений, /space_config, /report, /undo по показателю."""
import json
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import pytz
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.ext_db import ExtBase
from app.spaces import config as sc
from app.spaces import intake, ledger, metrics, repo, service
from app.spaces import personal as pers
from app.spaces import reminders as rm
from app.spaces.models import LedgerEntry

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
from app.handlers import intake as hi  # noqa: E402
from app.handlers import spaces as hs  # noqa: E402

OWNER, ADM, MEM, OUT = 101, 102, 103, 777
TZ = "Asia/Bangkok"
NOW = pytz.timezone(TZ).localize(datetime(2026, 11, 2, 7, 20))
TOYS = {"key": "toys", "title": "Погремушки", "unit": "шт", "kind": "count", "categories": ["Опт", "Розница"]}
CASH = {"key": "income", "title": "Выручка", "unit": "RUB", "kind": "money"}


def J(*items):
    return json.dumps({"items": list(items)}, ensure_ascii=False)


def mdict(*raw):
    return {m.key: m for m in metrics.parse_metrics(list(raw))[0]}


@pytest.fixture
def ctx():
    return intake.Context(now_local=NOW, tz=TZ, currency="RUB", kind="company", members={OWNER: "Шеф"},
                          categories=["Прочее"], metrics=mdict(TOYS, CASH))


def rec(**kw):
    return {**{"type": "record", "metric": "toys", "value": 12, "category": None, "note": "", "date": None}, **kw}


# ----------------------------- разбор record ------------------------------- #
class TestRecordParse:
    def test_ok_count_and_money(self, ctx):
        a, b = intake.parse_response(J(rec(value="12,5", category="опт"), rec(metric="income", value=1500.5)), ctx).proposals
        assert (a.metric, a.stored, a.unit, a.category) == ("toys", 1250, "шт", "Опт")  # регистр категории — из списка
        assert (b.metric, b.stored, b.unit, b.category) == ("income", 150050, "RUB", "")

    def test_unknown_metric_and_expense_key(self, ctx):
        parsed = intake.parse_response(J(rec(metric="nope"), rec(metric="expense")), ctx)
        assert parsed.proposals == [] and len(parsed.notes) == 2

    @pytest.mark.parametrize("value", [0, -3, "много", None, 10 ** 12, "1e30"])
    def test_bad_values(self, ctx, value):
        parsed = intake.parse_response(J(rec(value=value)), ctx)
        assert parsed.proposals == [] and parsed.notes

    def test_unknown_category_dropped_not_invented(self, ctx):
        [p] = intake.parse_response(J(rec(category="Оптом и в розницу")), ctx).proposals
        assert p.category == ""

    def test_category_ignored_when_metric_has_none(self, ctx):
        [p] = intake.parse_response(J(rec(metric="income", value=5, category="Опт")), ctx).proposals
        assert p.category == ""

    def test_date_bounds(self, ctx):
        [ok] = intake.parse_response(J(rec(date="2026-10-30")), ctx).proposals
        assert ok.occurred_at.startswith("2026-10-30")
        far = intake.parse_response(J(rec(date="2020-01-01")), ctx)
        assert far.proposals[0].occurred_at.startswith("2026-11-02T00:20") and far.notes  # «сейчас» 07:20 по Бангкоку = 00:20 UTC

    def test_without_custom_metrics_record_is_skipped(self):
        c = intake.Context(now_local=NOW, tz=TZ, currency="RUB", kind="family", members={}, categories=["Прочее"])
        assert intake.parse_response(J(rec()), c).proposals == []
        assert "record" not in intake.build_system_prompt(c)

    def test_prompt_lists_metrics_and_units(self, ctx):
        p = intake.build_system_prompt(ctx)
        assert "toys" in p and "Погремушки" in p and "шт" in p and "Опт, Розница" in p and "income" in p

    def test_render_uses_title(self, ctx):
        parsed = intake.parse_response(J(rec(value=3, category="Опт")), ctx)
        assert "Погремушки" in intake.render(parsed, titles={"toys": "Погремушки"})
        assert "3 шт" in intake.render(parsed).replace("\xa0", " ")


# ----------------------------- БД-часть ------------------------------------ #
@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (service, repo, rm, pers, intake):
        monkeypatch.setattr(mod, "resolve_session_maker", lambda _=None, m=maker: m)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def cfg(monkeypatch, sm):
    st = sys.modules["app.core.config"].settings
    for k, v in dict(SPACES_ENABLED=True, SPACE_INTAKE_ENABLED=True, SPACE_MAX_MEMBERS=10, ADMIN_IDS=[1],
                     BOT_OPEN_ACCESS=False, BOT_TIMEZONE=TZ, SPACE_REPORTS_ENABLED=True, SPACE_REPORT_DEFAULTS="monthly",
                     SPACE_REPORT_HOUR=9, SPACE_REPORT_PLUGINS="").items():
        monkeypatch.setattr(st, k, v, raising=False)


async def firm(settings=None):
    sp = await service.create_space("Фирма", OWNER, kind="company", timezone_name=TZ, owner_name="Шеф")
    await service.add_member(sp.id, ADM, role="admin", display_name="Зам")
    await service.add_member(sp.id, MEM, display_name="Сотрудник")
    await service.update_settings(sp.id, settings if settings is not None else {"metrics": [TOYS, CASH]})
    return await service.get_space(sp.id)


class TestRecordCommit:
    @pytest.mark.asyncio
    async def test_commit_stores_metric_and_shows_in_summary(self, sm, ctx):
        sp = await firm()
        scope = await repo.open_scope(sp.id, MEM)
        parsed = intake.parse_response(J(rec(value=12, category="Опт"), rec(value=3, category="Розница")), ctx)
        lines = await intake.commit(scope, sp, parsed.proposals, source="text", raw_text="продал")
        assert all(ln.startswith("✅") for ln in lines) and "12 шт" in lines[0].replace("\xa0", " ")
        rows = await scope.list(LedgerEntry)
        assert {(r.metric, r.amount_minor, r.currency) for r in rows} == {("toys", 1200, "шт"), ("toys", 300, "шт")}
        assert all(r.category_id for r in rows)
        s = await ledger.summarize(scope, metric="toys", unit="шт", start=datetime(2020, 1, 1), end=datetime(2100, 1, 1))
        assert s.total == 1500 and s.count == 2
        e = await ledger.summarize(scope, metric="expense", unit="RUB", start=datetime(2020, 1, 1), end=datetime(2100, 1, 1))
        assert e.count == 0  # расходы и показатели не смешиваются

    @pytest.mark.asyncio
    async def test_metric_removed_before_confirm(self, sm, ctx):
        sp = await firm()
        scope = await repo.open_scope(sp.id, MEM)
        parsed = intake.parse_response(J(rec()), ctx)
        await service.update_settings(sp.id, {"metrics": []})
        sp = await service.get_space(sp.id)
        [line] = await intake.commit(scope, sp, parsed.proposals, source="text", raw_text="x")
        assert line.startswith("❌") and not await scope.list(LedgerEntry)

    @pytest.mark.asyncio
    async def test_record_cannot_masquerade_as_expense(self, sm):
        sp = await firm()
        scope = await repo.open_scope(sp.id, MEM)
        forged = intake.RecordP(metric="expense", stored=100, unit="RUB")
        [line] = await intake.commit(scope, sp, [forged], source="text", raw_text="")
        assert line.startswith("❌") and not await scope.list(LedgerEntry)

    @pytest.mark.asyncio
    async def test_pending_roundtrip(self, sm, ctx):
        sp = await firm()
        parsed = intake.parse_response(J(rec(value=7, category="Опт", note="сдача")), ctx)
        token = await intake.put_pending(sp.id, MEM, parsed, "text", "raw")
        _, props, src, raw = await intake.take_pending(token, MEM)
        assert props == parsed.proposals and (src, raw) == ("text", "raw")

    @pytest.mark.asyncio
    async def test_make_context_has_custom_metrics_only(self, sm):
        sp = await firm()
        c = await intake.make_context(sp, await intake.member_names(sp.id), now=NOW)
        assert set(c.metrics) == {"toys", "income"}

    @pytest.mark.asyncio
    async def test_confirmation_policy(self, sm, ctx):
        sp = await firm()
        props = intake.parse_response(J(rec()), ctx).proposals
        assert intake.needs_confirmation(props, sp) is True
        await service.update_settings(sp.id, {"auto_confirm": ["shopping", "record"]})
        assert intake.needs_confirmation(props, await service.get_space(sp.id)) is False


class Msg:
    def __init__(self, uid, args=None):
        self.from_user = SimpleNamespace(id=uid, first_name="x", username="x")
        self.chat = SimpleNamespace(type="private", id=uid)
        self.text = ""
        self.args = args
        self.answer = AsyncMock()
        self.bot = MagicMock()


def said(m):
    return "\n".join(c.args[0] for c in m.answer.await_args_list).replace("\xa0", " ")


def cmd(args):
    return SimpleNamespace(args=args)


class TestUndoAndReport:
    @pytest.mark.asyncio
    async def test_undo_formats_by_metric(self, sm):
        sp = await firm()
        scope = await repo.open_scope(sp.id, MEM)
        await ledger.add_entry(scope, None, currency="шт", metric="toys", stored=1250, note="оптом")
        m = Msg(MEM)
        await hi.cmd_undo(m)
        assert "Погремушки" in said(m) and "12,50 шт" in said(m) and "оптом" in said(m)
        assert not await scope.list(LedgerEntry)

    @pytest.mark.asyncio
    async def test_undo_expense_unchanged(self, sm):
        sp = await firm()
        scope = await repo.open_scope(sp.id, MEM)
        await ledger.add_entry(scope, 300, currency="RUB", note="лампа")
        m = Msg(MEM)
        await hi.cmd_undo(m)
        assert "300" in said(m) and "Погремушки" not in said(m) and "Расходы" not in said(m)

    @pytest.mark.asyncio
    async def test_report_list_and_build(self, sm):
        reports = [{"id": "toys_m", "title": "Погремушки за месяц", "metric": "toys", "period": "month"},
                   {"id": "boss", "title": "Для начальства", "metric": "income", "audience": "admins"}]
        sp = await firm({"metrics": [TOYS, CASH], "reports": reports})
        scope = await repo.open_scope(sp.id, MEM)
        spec = next(s for s in __import__("app.spaces.reports", fromlist=["x"]).specs_for(sp) if s.id == "toys_m")
        rp = __import__("app.spaces.reports", fromlist=["x"])
        win = rp.window_for(spec, rp.slot_for(spec, datetime.now(pytz.utc).astimezone(pytz.timezone(TZ))), TZ)
        when = win.start + timedelta(days=9, hours=12)  # внутри отчётного периода при любой дате запуска
        await ledger.add_entry(scope, None, currency="шт", metric="toys", stored=500, occurred_at=when)
        m = Msg(MEM)
        await hi.cmd_report(m, cmd(None))
        assert "toys_m" in said(m) and "boss" not in said(m)  # адресованный админам — участнику не виден
        m2 = Msg(MEM)
        await hi.cmd_report(m2, cmd("toys_m"))
        assert "Погремушки за месяц" in said(m2) and "5 шт" in said(m2)
        m3 = Msg(MEM)
        await hi.cmd_report(m3, cmd("boss"))
        assert "Такого отчёта нет" in said(m3)
        m4 = Msg(ADM)
        await hi.cmd_report(m4, cmd(None))
        assert "boss" in said(m4)
        m5 = Msg(ADM)
        await hi.cmd_report(m5, cmd("boss"))
        assert "записей нет" in said(m5)  # период пуст — честный ответ, не пустое сообщение

    @pytest.mark.asyncio
    async def test_report_outsider_disabled_and_no_reports(self, sm, monkeypatch):
        sp = await firm({"reports": []})
        m = Msg(OUT)
        await hi.cmd_report(m, cmd(None))
        assert "не состоите" in said(m)
        m2 = Msg(MEM)
        await hi.cmd_report(m2, cmd(None))
        assert "Отчётов нет" in said(m2)
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "SPACES_ENABLED", False)
        m3 = Msg(MEM)
        await hi.cmd_report(m3, cmd(None))
        assert m3.answer.await_count == 0 and sp.id


# ----------------------------- валидаторы ---------------------------------- #
class TestValidators:
    @pytest.mark.parametrize("key,value", [
        ("metrics", [TOYS]), ("metrics", []), ("reports", [{"id": "a", "title": "A"}]),
        ("auto_confirm", ["shopping", "record", "shopping"]), ("auto_confirm", []), ("intake", False),
        ("categories", ["А", "Б"]), ("terms", {"ledger": "Продажи"}),
    ])
    def test_accepts(self, key, value):
        sc.prepare({}, key, value)

    @pytest.mark.parametrize("key,value", [
        ("metrics", "x"), ("metrics", [{"key": "Bad Key", "title": "t", "unit": "u"}]),
        ("metrics", [TOYS, TOYS]), ("metrics", [{"key": "expense", "title": "t", "unit": "u"}]),
        ("reports", {}), ("reports", [{"id": "a", "period": "year"}]), ("reports", [{"id": "a"}, {"id": "a"}]),
        ("auto_confirm", "shopping"), ("auto_confirm", ["delete_all"]), ("auto_confirm", [1]),
        ("intake", "false"), ("intake", 0), ("categories", []), ("categories", [" "]), ("categories", "x"),
        ("categories", ["я" * 51]), ("categories", [str(i) for i in range(31)]),
        ("terms", {"bogus": "x"}), ("terms", {"ledger": ""}), ("terms", {"ledger": "я" * 41}), ("terms", []),
        ("nope", 1), ("metrics", None),
    ])
    def test_rejects(self, key, value):
        with pytest.raises(sc.ConfigError):
            sc.prepare({}, key, value)

    def test_normalisation(self):
        assert sc.prepare({}, "auto_confirm", ["record", "record"])["auto_confirm"] == ["record"]
        assert sc.prepare({}, "categories", [" А  Б ", "А Б"])["categories"] == ["А Б"]

    def test_report_must_reference_known_metric(self):
        rep = [{"id": "r", "metric": "toys"}]
        with pytest.raises(sc.ConfigError, match="toys"):
            sc.prepare({}, "reports", rep)
        assert sc.prepare({"metrics": [TOYS]}, "reports", rep)["reports"] == rep
        sc.prepare({}, "reports", [{"id": "e"}])  # расходы есть всегда

    def test_cannot_drop_metric_used_by_report_but_can_replace(self):
        cur = {"metrics": [TOYS], "reports": [{"id": "r", "metric": "toys"}]}
        with pytest.raises(sc.ConfigError):
            sc.prepare(cur, "metrics", [CASH])
        with pytest.raises(sc.ConfigError):
            sc.prepare_unset(cur, "metrics")
        assert sc.prepare(cur, "metrics", [TOYS, CASH])["metrics"][1]["key"] == "income"
        assert "reports" not in sc.prepare_unset(cur, "reports")

    def test_size_limit_and_no_mutation(self):
        cur = {"categories": ["а"]}
        big = {"terms": {"ledger": "x"}}
        assert sc.prepare(cur, "terms", big["terms"]) and cur == {"categories": ["а"]}
        with pytest.raises(sc.ConfigError, match="байт"):
            sc.prepare({"reports": [{"id": "r", "title": "я" * 60, "options": {"x": "я" * 9000}}]}, "intake", True)

    def test_unset_only_known(self):
        with pytest.raises(sc.ConfigError):
            sc.prepare_unset({}, "timezone")
        assert sc.prepare_unset({"intake": False}, "intake") == {}

    @pytest.mark.parametrize("key,val,out", [("timezone", "Europe/Moscow", "Europe/Moscow"), ("currency", " usd ", "USD")])
    def test_columns_ok(self, key, val, out):
        assert sc.validate_column(key, val) == out

    @pytest.mark.parametrize("key,val", [("timezone", "Mars/Base"), ("timezone", 3), ("currency", "RUBL"), ("currency", "р"),
                                         ("currency", "R1B")])
    def test_columns_bad(self, key, val):
        with pytest.raises(sc.ConfigError):
            sc.validate_column(key, val)

    def test_parse_command(self):
        assert sc.parse_command("") == ("show", "", "")
        assert sc.parse_command(' SET Intake  false') == ("set", "intake", "false")
        assert sc.parse_command('set metrics [{"a": 1, "b": 2}]')[2] == '[{"a": 1, "b": 2}]'
        assert sc.parse_command("unset terms") == ("unset", "terms", "")
        for bad in ("set", "set x", "unset", "drop x", "x"):
            with pytest.raises(sc.ConfigError):
                sc.parse_command(bad)

    def test_load_value(self):
        assert sc.load_value("intake", "false") is False
        assert sc.load_value("timezone", "Europe/Moscow") == "Europe/Moscow"  # колонки — без кавычек
        with pytest.raises(sc.ConfigError):
            sc.load_value("metrics", "[oops")

    def test_render_truncates(self):
        out = sc.render({"categories": ["я" * 50] * 30}, limit=100)
        assert len(out) <= 102 and out.endswith("…")


# ----------------------------- /space_config ------------------------------- #
class TestSpaceConfigHandler:
    @pytest.mark.asyncio
    async def test_members_and_outsiders_refused(self, sm):
        sp = await firm()
        for uid in (MEM, OUT):
            m = Msg(uid)
            await hs.cmd_space_config(m, cmd('set intake false'))
            assert "Доступно" in said(m) or "не состоите" in said(m) or "⛔" in said(m)
        assert (await service.get_space(sp.id)).settings.get("intake") is None

    @pytest.mark.asyncio
    async def test_show_escapes_html(self, sm):
        sp = await firm({"terms": {"ledger": "<b>&"}})
        m = Msg(ADM)
        await hs.cmd_space_config(m, cmd(None))
        out = said(m)
        assert "&lt;b&gt;" in out and "<b>&" not in out and sp.name in out and "timezone" in out

    @pytest.mark.asyncio
    async def test_set_unset_cycle(self, sm):
        sp = await firm({})
        m = Msg(ADM)
        await hs.cmd_space_config(m, cmd("set metrics " + json.dumps([TOYS], ensure_ascii=False)))
        assert "✅" in said(m)
        assert "toys" in metrics.metrics_for(await service.get_space(sp.id))
        await hs.cmd_space_config(m, cmd("set reports " + json.dumps([{"id": "r", "metric": "toys"}])))
        m2 = Msg(ADM)
        await hs.cmd_space_config(m2, cmd("unset metrics"))  # отчёт ссылается — нельзя
        assert "❌" in said(m2) and "toys" in metrics.metrics_for(await service.get_space(sp.id))
        await hs.cmd_space_config(Msg(ADM), cmd("unset reports"))
        await hs.cmd_space_config(Msg(ADM), cmd("unset metrics"))
        assert (await service.get_space(sp.id)).settings == {}

    @pytest.mark.asyncio
    async def test_invalid_input_changes_nothing(self, sm):
        sp = await firm()
        before = dict((await service.get_space(sp.id)).settings)
        for args in ("set metrics [oops", 'set metrics "x"', "set auto_confirm [\"rm\"]", "set bogus 1", "set intake",
                     "unset timezone", "frobnicate", "set timezone Mars/Base", "set currency EURO"):
            m = Msg(ADM)
            await hs.cmd_space_config(m, cmd(args))
            assert "❌" in said(m), args
        after = await service.get_space(sp.id)
        assert after.settings == before and (after.timezone, after.currency) == (TZ, "RUB")

    @pytest.mark.asyncio
    async def test_columns(self, sm):
        sp = await firm()
        m = Msg(OWNER)
        await hs.cmd_space_config(m, cmd("set timezone Europe/Moscow"))
        await hs.cmd_space_config(m, cmd("set currency usd"))
        got = await service.get_space(sp.id)
        assert (got.timezone, got.currency) == ("Europe/Moscow", "USD") and got.settings == {"metrics": [TOYS, CASH]}
        assert "не попадут" in said(m)

    @pytest.mark.asyncio
    async def test_disabled(self, sm, monkeypatch):
        await firm()
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "SPACES_ENABLED", False)
        m = Msg(ADM)
        await hs.cmd_space_config(m, cmd(None))
        assert m.answer.await_count == 0

    @pytest.mark.asyncio
    async def test_config_then_intake_flow(self, sm):
        """Сквозной путь: админ описал показатель → LLM-черновик → подтверждение → запись видна в сводке."""
        sp = await firm({})
        await hs.cmd_space_config(Msg(ADM), cmd("set metrics " + json.dumps([TOYS], ensure_ascii=False)))
        sp = await service.get_space(sp.id)
        c = await intake.make_context(sp, await intake.member_names(sp.id), now=NOW)
        parsed = intake.parse_response(J(rec(value=40, category="Опт")), c)
        scope = await repo.open_scope(sp.id, MEM)
        await intake.commit(scope, sp, parsed.proposals, source="text", raw_text="")
        s = await ledger.summarize(scope, metric="toys", unit="шт", start=datetime(2020, 1, 1), end=datetime(2100, 1, 1))
        assert s.total == 4000
