"""Отчёты пространств: показатели, описания, слоты расписания, окна, отправка ровно один раз, плагины."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import pytz
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.ext_db import ExtBase
from app.spaces import ledger, metrics, personal, reminders, repo, reports, service
from app.spaces.models import LedgerEntry, ReportRun

DAD, MOM, KID = 101, 102, 103
TZ = "Asia/Bangkok"
BKK = pytz.timezone(TZ)


def local(y, mo, d, h=0, mi=0):
    return BKK.localize(datetime(y, mo, d, h, mi))


def utc(dt_local):
    return dt_local.astimezone(timezone.utc)


# =============================== показатели ================================= #
class TestMetrics:
    def test_parse_valid_and_invalid(self):
        ms, errs = metrics.parse_metrics([
            {"key": "rattles", "title": "Проданные погремушки", "unit": "шт", "kind": "count"},
            {"key": "Rattles", "title": "дубль регистра", "unit": "шт"},
            {"key": "expense", "title": "занято", "unit": "x"},
            {"key": "1bad", "title": "t", "unit": "u"},
            {"key": "nounit", "title": "t", "unit": ""},
            {"key": "badkind", "title": "t", "unit": "u", "kind": "weird"},
            "строка",
            {"key": "hours", "title": "Часы", "unit": "ч" * 20, "kind": "count"},
        ])
        assert [m.key for m in ms] == ["rattles", "hours"] and len(errs) == 6
        assert ms[1].unit == "ч" * 8  # единица обрезается
        assert metrics.parse_metrics(None) == ([], []) and metrics.parse_metrics("x")[1]

    def test_limit(self):
        raw = [{"key": f"m{i}", "title": "t", "unit": "u"} for i in range(30)]
        ms, errs = metrics.parse_metrics(raw)
        assert len(ms) == metrics.MAX_METRICS and any("больше" in e for e in errs)

    def test_registry_always_has_expense(self):
        sp = type("S", (), {"currency": "rub", "settings": {"metrics": [{"key": "k", "title": "К", "unit": "шт"}]}})
        got = metrics.metrics_for(sp)
        assert got["expense"].unit == "RUB" and got["expense"].kind == "money" and "k" in got
        bad = type("S", (), {"currency": "RUB", "settings": "junk"})
        assert list(metrics.metrics_for(bad)) == ["expense"]

    def test_stored_and_format(self):
        cnt, money = metrics.Metric("k", "К", "шт", "count"), metrics.default_expense("RUB")
        assert metrics.to_stored(cnt, "12") == 1200 and metrics.to_stored(cnt, "0,5") == 50
        assert metrics.to_stored(money, "1 200,50") == 120050
        for bad in (0, -1, "много", "1e30", "nan", None, "0.001"):
            with pytest.raises(ledger.LedgerError):
                metrics.to_stored(cnt, bad)
        assert metrics.formatter(cnt)(1200) == "12 шт" and metrics.formatter(cnt)(50) == "0,50 шт"
        assert metrics.formatter(cnt)(123400) == "1 234 шт"
        assert metrics.formatter(money)(120050) == "1 200,50 ₽"


# =============================== описания =================================== #
class TestParseSpecs:
    def test_valid_variants(self):
        specs, errs = reports.parse_specs([
            {"id": "m", "metric": "rattles", "period": "month", "day": 5, "time": "10:30", "title": "Погремушки"},
            {"id": "w", "period": "week", "weekday": "fri", "audience": "admins", "send_empty": True},
            {"id": "w2", "period": "week", "weekday": 6},
            {"id": "off", "enabled": False},
        ])
        assert errs == [] and [s.id for s in specs] == ["m", "w", "w2"]
        assert (specs[0].day, specs[0].hour, specs[0].minute, specs[0].metric) == (5, 10, 30, "rattles")
        assert (specs[1].weekday, specs[1].audience, specs[1].send_empty) == (4, "admins", True) and specs[2].weekday == 6

    @pytest.mark.parametrize("bad", [
        {"id": "Bad ID"}, {"id": "x", "period": "year"}, {"id": "x", "day": 31}, {"id": "x", "day": True},
        {"id": "x", "period": "week", "weekday": "xyz"}, {"id": "x", "time": "25:00"}, {"id": "x", "time": "9"},
        {"id": "x", "audience": "everyone"}, {"id": "x", "type": "unknown"}, "строка", 5,
    ])
    def test_invalid_dropped_with_error(self, bad):
        specs, errs = reports.parse_specs([bad])
        assert specs == [] and len(errs) == 1

    def test_duplicates_and_limit_and_not_a_list(self):
        specs, errs = reports.parse_specs([{"id": "a"}, {"id": "a"}])
        assert len(specs) == 1 and errs
        specs, errs = reports.parse_specs([{"id": f"r{i}"} for i in range(15)])
        assert len(specs) == reports.MAX_REPORTS and any("больше" in e for e in errs)
        assert reports.parse_specs({"id": "a"})[0] == []

    def test_plugin_types(self, monkeypatch):
        monkeypatch.setattr(reports, "BUILDERS", {})
        assert reports.parse_specs([{"id": "x", "type": "custom"}])[0] == []
        reports.register_report_type("custom", AsyncMock())
        [s] = reports.parse_specs([{"id": "x", "type": "custom", "options": {"a": 1}}])[0]
        assert s.type == "custom" and s.options == {"a": 1}
        for bad in ("metric", "Bad Name", ""):
            with pytest.raises(ValueError):
                reports.register_report_type(bad, AsyncMock())


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    st = sys.modules["app.core.config"].settings
    for k, v in dict(SPACE_REPORTS_ENABLED=True, SPACE_REPORT_DEFAULTS="monthly", SPACE_REPORT_HOUR=9,
                     SPACE_REPORT_PLUGINS="", SPACE_MAX_MEMBERS=10, SPACES_ENABLED=True).items():
        monkeypatch.setattr(st, k, v, raising=False)
    reports._done.clear()
    reports._warned.clear()
    yield
    reports._done.clear()


class TestEnvDefaults:
    def test_defaults(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        assert [s.id for s in reports.default_specs()] == ["monthly"]
        monkeypatch.setattr(st, "SPACE_REPORT_DEFAULTS", " Weekly , MONTHLY ,junk")
        assert [s.id for s in reports.default_specs()] == ["monthly", "weekly"]
        monkeypatch.setattr(st, "SPACE_REPORT_DEFAULTS", "")
        assert reports.default_specs() == []
        monkeypatch.setattr(st, "SPACE_REPORT_DEFAULTS", 5)
        assert reports.default_specs() == []
        monkeypatch.setattr(st, "SPACE_REPORT_DEFAULTS", "monthly")
        monkeypatch.setattr(st, "SPACE_REPORT_HOUR", "x")
        assert reports.default_specs()[0].hour == 9
        monkeypatch.setattr(st, "SPACE_REPORT_HOUR", 18)
        assert reports.default_specs()[0].hour == 18

    def test_specs_for_precedence(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        sp = type("S", (), {"id": 1, "settings": {}})
        assert [s.id for s in reports.specs_for(sp)] == ["monthly"]  # умолчания из .env
        sp.settings = {"reports": []}
        assert reports.specs_for(sp) == []  # пустой список в настройках — отчётов нет
        sp.settings = {"reports": [{"id": "own", "period": "week"}]}
        assert [s.id for s in reports.specs_for(sp)] == ["own"]
        sp.settings = {"reports": [{"id": "bad id"}]}
        assert reports.specs_for(sp) == []
        monkeypatch.setattr(st, "SPACE_REPORTS_ENABLED", False)
        sp.settings = {"reports": [{"id": "own"}]}
        assert reports.specs_for(sp) == []  # глобальный выключатель сильнее всего
        monkeypatch.setattr(st, "SPACE_REPORTS_ENABLED", "false")
        assert reports.specs_for(sp) == [] and not reports.enabled()


# =============================== слоты и окна =============================== #
class TestSlots:
    def spec(self, **kw):
        return reports.ReportSpec(id="r", title="T", **kw)

    def test_monthly_slot(self):
        s = self.spec(period="month", day=1, hour=9)
        assert reports.slot_for(s, local(2026, 11, 1, 9, 0)) == local(2026, 11, 1, 9)
        assert reports.slot_for(s, local(2026, 11, 1, 8, 59)) == local(2026, 10, 1, 9)  # ещё не время
        assert reports.slot_for(s, local(2026, 11, 20, 12)) == local(2026, 11, 1, 9)
        assert reports.slot_for(self.spec(day=15), local(2026, 1, 10)) == local(2025, 12, 15, 9)  # через год
        assert reports.slot_key(s, local(2026, 11, 1, 9)) == "month:2026-11-01"

    def test_weekly_slot(self):
        s = self.spec(period="week", weekday=0, hour=9)  # понедельник
        assert reports.slot_for(s, local(2026, 11, 2, 9, 30)) == local(2026, 11, 2, 9)  # понедельник
        assert reports.slot_for(s, local(2026, 11, 2, 8)) == local(2026, 10, 26, 9)
        assert reports.slot_for(s, local(2026, 11, 8, 23)) == local(2026, 11, 2, 9)  # воскресенье

    def test_month_window_uses_previous_month_local(self):
        win = reports.window_for(self.spec(period="month"), local(2026, 11, 1, 9), TZ)
        assert (win.start, win.end) == (datetime(2026, 9, 30, 17), datetime(2026, 10, 31, 17))
        assert win.label == "октябрь 2026" and win.days == 31
        assert (win.prev_start, win.prev_end) == (datetime(2026, 8, 31, 17), datetime(2026, 9, 30, 17))
        jan = reports.window_for(self.spec(period="month"), local(2026, 1, 1, 9), "UTC".replace("UTC", TZ))
        assert jan.label == "декабрь 2025"

    def test_week_window(self):
        win = reports.window_for(self.spec(period="week"), local(2026, 11, 2, 9), TZ)  # слот — понедельник 2.11
        assert win.label == "26.10–01.11" and win.days == 7
        assert (win.start, win.end) == (datetime(2026, 10, 25, 17), datetime(2026, 11, 1, 17))
        assert (win.prev_start, win.prev_end) == (datetime(2026, 10, 18, 17), datetime(2026, 10, 25, 17))
        fri = reports.window_for(self.spec(period="week", weekday=4), local(2026, 11, 6, 9), TZ)  # пятница
        assert fri.label == "26.10–01.11"  # всё равно прошлая полная неделя


# =============================== исполнение ================================= #
@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (service, repo, reminders, personal, reports):
        monkeypatch.setattr(mod, "resolve_session_maker", lambda _=None, m=maker: m)
    from app.spaces import intake
    monkeypatch.setattr(intake, "resolve_session_maker", lambda _=None, m=maker: m)
    yield maker
    await engine.dispose()


class Outbox:
    def __init__(self, fail=()):
        self.sent, self.fail = [], set(fail)

    async def __call__(self, chat_id, text):
        if chat_id in self.fail:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


async def fam(settings=None, tz=TZ):
    sp = await service.create_space("Ивановы", DAD, kind="family", timezone_name=tz, owner_name="Папа",
                                    settings=settings or {})
    await service.add_member(sp.id, MOM, display_name="Мама")
    await service.add_member(sp.id, KID, display_name="Саша", role="member")
    sc = await repo.open_scope(sp.id, DAD)
    await ledger.ensure_categories(sc, "family")
    return sp, sc


async def spend(scope, amount, when, category="Продукты", metric="expense", unit="RUB", stored=None):
    return await ledger.add_entry(scope, amount, currency=unit, category=category, occurred_at=when,
                                  metric=metric, stored=stored)


NOV1 = utc(local(2026, 11, 1, 9, 5))


class TestRunDue:
    @pytest.mark.asyncio
    async def test_monthly_report_sent_once_to_everyone(self, sm):
        sp, sc = await fam()
        await spend(sc, 1000, datetime(2026, 10, 10))
        await spend(sc, 3000, datetime(2026, 10, 20), category="Дом")
        await spend(sc, 999, datetime(2026, 11, 1, 3))  # ноябрь — в отчёт за октябрь не входит
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 1
        assert sorted(c for c, _ in out.sent) == [DAD, MOM, KID]
        text = out.sent[0][1]
        assert "октябрь 2026" in text and "4 000 ₽" in text and "999" not in text
        assert await reports.run_due(out, NOV1 + timedelta(minutes=5)) == 0 and len(out.sent) == 3
        reports._done.clear()  # «перезапуск процесса»: журнал в БД не даёт отправить второй раз
        assert await reports.run_due(out, NOV1 + timedelta(hours=1)) == 0 and len(out.sent) == 3

    @pytest.mark.asyncio
    async def test_next_month_is_a_new_slot(self, sm):
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))
        await spend(sc, 200, datetime(2026, 11, 10))
        out = Outbox()
        await reports.run_due(out, NOV1)
        await reports.run_due(out, utc(local(2026, 12, 1, 9, 1)))
        assert len(out.sent) == 6 and "ноябрь 2026" in out.sent[-1][1]
        assert "К прошлому периоду" in out.sent[-1][1] and "▲" in out.sent[-1][1]

    @pytest.mark.asyncio
    async def test_empty_period_is_silent_by_default(self, sm):
        await fam()
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 0 and out.sent == []
        async with sm() as s:
            assert (await s.execute(__import__("sqlalchemy").select(ReportRun))).scalars().one().status == "skipped"

    @pytest.mark.asyncio
    async def test_send_empty_flag(self, sm):
        await fam({"reports": [{"id": "m", "send_empty": True, "title": "Расходы"}]})
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 1 and "нет" in out.sent[0][1]

    @pytest.mark.asyncio
    async def test_stale_slot_is_not_sent_late(self, sm):
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))
        out = Outbox()
        # отчёты включили 15-го: слот 1 ноября давно прошёл — задним числом не шлём, но и не вспоминаем
        assert await reports.run_due(out, utc(local(2026, 11, 15, 12))) == 0 and out.sent == []
        reports._done.clear()
        assert await reports.run_due(out, utc(local(2026, 11, 15, 13))) == 0 and out.sent == []

    @pytest.mark.asyncio
    async def test_downtime_sends_only_latest_slot(self, sm):
        sp, sc = await fam({"reports": [{"id": "w", "period": "week", "weekday": "mon"}]})
        await spend(sc, 100, datetime(2026, 11, 1, 5))
        out = Outbox()
        # бот поднялся во вторник утром, через сутки после понедельничного слота — ещё в пределах допуска
        assert await reports.run_due(out, utc(local(2026, 11, 3, 8))) == 1 and len(out.sent) == 3
        assert "26.10–01.11" in out.sent[0][1]

    @pytest.mark.asyncio
    async def test_disabled_everywhere(self, sm, monkeypatch):
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "SPACE_REPORTS_ENABLED", False)
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 0 and out.sent == []

    @pytest.mark.asyncio
    async def test_space_can_opt_out_and_inactive_is_silent(self, sm):
        sp, sc = await fam({"reports": []})
        await spend(sc, 100, datetime(2026, 10, 10))
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 0
        sp2, sc2 = await fam()
        await spend(sc2, 100, datetime(2026, 10, 10))
        await service.deactivate_space(sp2.id)
        assert await reports.run_due(out, NOV1) == 0 and out.sent == []

    @pytest.mark.asyncio
    async def test_personal_space_gets_no_reports(self, sm):
        sc = await personal.personal_scope(DAD, TZ)
        await ledger.ensure_categories(sc, "personal")
        await spend(sc, 100, datetime(2026, 10, 10))
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 0 and out.sent == []

    @pytest.mark.asyncio
    async def test_audience_admins(self, sm):
        sp, sc = await fam({"reports": [{"id": "m"}, {"id": "adm", "audience": "admins", "title": "Для своих"}]})
        await spend(sc, 100, datetime(2026, 10, 10))
        await service.set_role(sp.id, MOM, "admin")
        out = Outbox()
        await reports.run_due(out, NOV1)
        adm = [c for c, t in out.sent if "Для своих" in t]
        assert sorted(adm) == [DAD, MOM] and len([1 for _, t in out.sent if "Для своих" not in t]) == 3

    @pytest.mark.asyncio
    async def test_timezones_are_per_space(self, sm):
        # в Бангкоке 1 ноября 09:05 уже наступило, а в Нью-Йорке (UTC−5) ещё 31 октября 21:05
        _, bk = await fam()
        _, ny = await fam(tz="America/New_York")
        await spend(bk, 100, datetime(2026, 10, 10))
        await spend(ny, 100, datetime(2026, 10, 10))
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 1  # только Бангкок

    @pytest.mark.asyncio
    async def test_failed_delivery_is_retried_then_stops(self, sm):
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))
        bad = Outbox(fail={DAD, MOM, KID})
        for i in range(reports.MAX_ATTEMPTS + 2):
            assert await reports.run_due(bad, NOV1 + timedelta(minutes=i)) == 0  # доставленных нет — не считается
        async with sm() as s:
            row = (await s.execute(__import__("sqlalchemy").select(ReportRun))).scalars().one()
        assert (row.status, row.attempts) == ("failed", reports.MAX_ATTEMPTS)
        reports._done.clear()
        good = Outbox()
        assert await reports.run_due(good, NOV1 + timedelta(minutes=30)) == 0  # попытки исчерпаны
        # а один недоступный адресат не мешает остальным
        sp2, sc2 = await fam()
        await spend(sc2, 100, datetime(2026, 10, 10))
        part = Outbox(fail={MOM})
        assert await reports.run_due(part, NOV1 + timedelta(hours=1)) == 1
        assert sorted(c for c, _ in part.sent) == [DAD, KID]

    @pytest.mark.asyncio
    async def test_failed_then_recovered(self, sm):
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))
        await reports.run_due(Outbox(fail={DAD, MOM, KID}), NOV1)
        good = Outbox()
        assert await reports.run_due(good, NOV1 + timedelta(minutes=1)) == 1 and len(good.sent) == 3

    @pytest.mark.asyncio
    async def test_parallel_workers_send_once(self, tmp_path, monkeypatch):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(ExtBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        from app.spaces import intake
        for mod in (service, repo, reminders, personal, reports, intake):
            monkeypatch.setattr(mod, "resolve_session_maker", lambda _=None, m=maker: m)
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))
        out = Outbox()
        await asyncio.gather(*(reports.run_due(out, NOV1) for _ in range(4)))
        await engine.dispose()
        assert len(out.sent) == 3

    @pytest.mark.asyncio
    async def test_closed_slot_is_not_rechecked_in_db(self, sm, monkeypatch):
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))
        await reports.run_due(Outbox(), NOV1)
        spy = AsyncMock(return_value=False)
        monkeypatch.setattr(reports, "_claim", spy)
        await reports.run_due(Outbox(), NOV1 + timedelta(minutes=1))
        await reports.run_due(Outbox(), NOV1 + timedelta(minutes=2))
        spy.assert_not_awaited()  # раз в минуту ходить в БД за уже закрытым слотом незачем

    @pytest.mark.asyncio
    async def test_parallel_retry_of_failed_slot_happens_once(self, tmp_path, monkeypatch):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(ExtBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        from app.spaces import intake
        for mod in (service, repo, reminders, personal, reports, intake):
            monkeypatch.setattr(mod, "resolve_session_maker", lambda _=None, m=maker: m)
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))
        await reports.run_due(Outbox(fail={DAD, MOM, KID}), NOV1)  # первая попытка неудачна
        reports._done.clear()
        out = Outbox()
        await asyncio.gather(*(reports.run_due(out, NOV1 + timedelta(minutes=1)) for _ in range(4)))
        await engine.dispose()
        assert len(out.sent) == 3  # повтор выполнил один из четырёх, а не все

    @pytest.mark.asyncio
    async def test_never_raises(self, sm, monkeypatch):
        monkeypatch.setattr(reports, "_active_spaces", AsyncMock(side_effect=RuntimeError("db")))
        assert await reports.run_due(Outbox(), NOV1) == 0
        await fam()
        monkeypatch.undo()


class TestCustomMetric:
    @pytest.mark.asyncio
    async def test_counts_report(self, sm):
        settings = {
            "metrics": [{"key": "rattles", "title": "Проданные погремушки", "unit": "шт", "kind": "count"}],
            "reports": [{"id": "sales", "metric": "rattles", "period": "week", "weekday": "mon",
                         "title": "Продажи погремушек"}],
        }
        sp, sc = await fam(settings)
        mom = await repo.open_scope(sp.id, MOM)
        await ledger.ensure_categories(sc, "family")
        await spend(sc, None, datetime(2026, 10, 27, 5), category="Дом", metric="rattles", unit="шт", stored=1200)
        await spend(mom, None, datetime(2026, 10, 28, 5), category="Дом", metric="rattles", unit="шт", stored=800)
        await spend(sc, 5000, datetime(2026, 10, 28, 5))  # расход — другой показатель, в сводку не попадёт
        await spend(sc, None, datetime(2026, 10, 20, 5), metric="rattles", unit="шт", stored=1000)  # прошлая неделя
        out = Outbox()
        assert await reports.run_due(out, utc(local(2026, 11, 2, 9, 5))) == 1
        text = out.sent[0][1]
        assert "Продажи погремушек: 26.10–01.11" in text and "20 шт" in text  # 12 + 8
        assert "₽" not in text and "5 000" not in text
        assert "К прошлому периоду: ▲ 10 шт (+100%)" in text
        assert "Кто вносил" in text and "Папа — 12 шт" in text and "Мама — 8 шт" in text

    @pytest.mark.asyncio
    async def test_undefined_metric_fails_loudly_not_silently(self, sm, caplog):
        sp, sc = await fam({"reports": [{"id": "x", "metric": "ghost"}]})
        out = Outbox()
        import logging
        with caplog.at_level(logging.ERROR):
            await reports.run_due(out, NOV1)
        assert out.sent == [] and "ghost" in caplog.text

    @pytest.mark.asyncio
    async def test_units_do_not_mix(self, sm):
        sp, sc = await fam()
        await spend(sc, 100, datetime(2026, 10, 10))  # RUB
        await spend(sc, 7, datetime(2026, 10, 11), unit="USD")
        out = Outbox()
        await reports.run_due(out, NOV1)
        assert "100 ₽" in out.sent[0][1] and "USD" not in out.sent[0][1]


class TestPlugins:
    @pytest.mark.asyncio
    async def test_custom_report_type(self, sm, monkeypatch):
        monkeypatch.setattr(reports, "BUILDERS", {})
        seen = {}

        async def my_builder(scope, space, spec, win):
            seen.update(space=space.id, spec=spec.options, label=win.label,
                        n=await scope.count(LedgerEntry))
            return "<b>свой отчёт</b>"

        reports.register_report_type("mine", my_builder)
        sp, sc = await fam({"reports": [{"id": "c", "type": "mine", "options": {"top": 3}}]})
        await spend(sc, 100, datetime(2026, 10, 10))
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 1
        assert out.sent[0][1] == "<b>свой отчёт</b>" and seen["spec"] == {"top": 3} and seen["n"] == 1

    @pytest.mark.asyncio
    async def test_builder_returning_none_skips_and_crash_is_contained(self, sm, monkeypatch, caplog):
        monkeypatch.setattr(reports, "BUILDERS", {})
        reports.register_report_type("none", AsyncMock(return_value=None))
        reports.register_report_type("boom", AsyncMock(side_effect=RuntimeError("плагин упал")))
        await fam({"reports": [{"id": "n", "type": "none"}, {"id": "b", "type": "boom"}, {"id": "m"}]})
        out = Outbox()
        assert await reports.run_due(out, NOV1) == 0  # «m» пуст, «n» пропущен, «b» упал — остальные не пострадали
        assert "плагин упал" in caplog.text

    def test_load_plugins(self, tmp_path, monkeypatch, caplog):
        (tmp_path / "myplugin.py").write_text(
            "from app.spaces import reports\n"
            "async def b(scope, space, spec, win):\n    return 'x'\n"
            "reports.register_report_type('fromfile', b)\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setattr(reports, "BUILDERS", {})
        assert reports.load_plugins("myplugin, no_such_module ,") == ["myplugin"]
        assert "fromfile" in reports.BUILDERS and "no_such_module" in caplog.text
        assert reports.load_plugins("") == []


class TestWiring:
    @pytest.mark.asyncio
    async def test_loop_runs_hooks_and_survives_hook_failure(self, sm):
        calls = {"good": 0}

        async def good():
            calls["good"] += 1

        async def bad():
            raise RuntimeError("x")

        stop = asyncio.Event()
        task = asyncio.create_task(reminders.run_loop(Outbox(), 0.05, stop, hooks=[(bad, 0.0), (good, 0.0)]))
        await asyncio.sleep(0.25)
        stop.set()
        await asyncio.wait_for(task, 2)
        assert calls["good"] >= 2

    @pytest.mark.asyncio
    async def test_hook_interval_is_respected(self, sm):
        calls = {"n": 0}

        async def hook():
            calls["n"] += 1

        stop = asyncio.Event()
        task = asyncio.create_task(reminders.run_loop(Outbox(), 0.02, stop, hooks=[(hook, 3600.0)]))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 2)
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_runner_passes_report_hook_only_when_enabled(self, monkeypatch):
        from app.spaces import runner

        captured = {}

        async def fake_loop(send, interval, stop, hooks=()):
            captured["hooks"] = hooks

        monkeypatch.setattr(reminders, "run_loop", fake_loop)
        monkeypatch.setattr(reports, "load_plugins", lambda csv: captured.setdefault("plugins", csv) or [])
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "SPACE_REPORT_PLUGINS", "my.mod")
        await runner.start(AsyncMock())
        runner._task = None
        assert len(captured["hooks"]) == 1 and captured["plugins"] == "my.mod"
        monkeypatch.setattr(st, "SPACE_REPORTS_ENABLED", False)
        await runner.start(AsyncMock())
        runner._task = None
        assert captured["hooks"] == []

    def test_models_registered(self):
        assert "report_runs" in ExtBase.metadata.tables
        assert "metric" in ExtBase.metadata.tables["ledger_entries"].columns


class TestLedgerBackCompat:
    @pytest.mark.asyncio
    async def test_old_expense_flow_untouched(self, sm):
        sp, sc = await fam()
        e = await ledger.add_entry(sc, 150, currency="RUB", category="Дом")
        assert e.metric == "expense"
        s = await ledger.monthly_summary(sc, 2026, 11, tz=TZ, currency="RUB")
        assert isinstance(s, ledger.MonthSummary) and (s.year, s.month, s.currency) == (2026, 11, "RUB")

    @pytest.mark.asyncio
    async def test_custom_metric_never_leaks_into_expenses(self, sm):
        sp, sc = await fam()
        await spend(sc, None, datetime(2026, 11, 3), metric="rattles", unit="RUB", stored=999900)
        s = await ledger.monthly_summary(sc, 2026, 11, tz=TZ, currency="RUB")
        assert s.total == 0
