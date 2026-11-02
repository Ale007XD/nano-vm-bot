"""Личные напоминания (/remind, /daily, /weekly) по новой схеме: текстом без LLM, через исполнитель пространств."""
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.ext_db import ExtBase
from app.spaces import personal, repo, service
from app.spaces import reminders as rm

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
from app.handlers import reminders as h  # noqa: E402

ME, OTHER, DAD = 7, 8, 9
TZ = "Asia/Bangkok"


@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (service, repo, rm, personal):
        monkeypatch.setattr(mod, "resolve_session_maker", lambda _=None, m=maker: m)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    st = sys.modules["app.core.config"].settings
    for k, v in dict(SPACES_ENABLED=True, SPACE_MAX_MEMBERS=10, SPACE_INVITE_TTL_DAYS=7, ADMIN_IDS=[1],
                     BOT_OPEN_ACCESS=False, BOT_TIMEZONE=TZ).items():
        monkeypatch.setattr(st, k, v, raising=False)
    monkeypatch.setattr(sys.modules["app.core.database"], "is_user_banned", AsyncMock(return_value=False))
    return st


def soon(minutes=30):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


class Outbox:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


class TestPersonalSpace:
    @pytest.mark.asyncio
    async def test_created_once_per_user(self, sm):
        a = await personal.personal_scope(ME, TZ)
        assert (await personal.personal_scope(ME, TZ)).space_id == a.space_id
        assert (await personal.personal_scope(OTHER, TZ)).space_id != a.space_id
        sp = await service.get_space(a.space_id)
        assert (sp.kind, sp.timezone, a.role) == ("personal", TZ, "owner")

    @pytest.mark.asyncio
    async def test_gives_no_bot_access(self, sm):
        from app.core.access import user_allowed

        await personal.personal_scope(ME, TZ)
        assert not await service.is_member_anywhere(ME)
        assert not await user_allowed(ME)  # закрытый бот остаётся закрытым
        assert await repo.scope_for_user(ME) is None  # команды общих пространств его не видят
        assert len(await service.spaces_of(ME, include_personal=True)) == 1

    @pytest.mark.asyncio
    async def test_cannot_be_shared(self, sm):
        sc = await personal.personal_scope(ME, TZ)
        assert await service.add_member(sc.space_id, OTHER) == service.RESULT_INVALID
        with pytest.raises(service.SpaceError):
            await service.create_invite(sc.space_id, ME)
        code_space = await service.create_space("Ивановы", ME, kind="family")
        assert (await repo.scope_for_user(ME)).space_id == code_space.id  # общее важнее личного

    @pytest.mark.asyncio
    async def test_cron_helpers(self):
        assert personal.daily_cron(9, 5) == "5 9 * * *"
        assert personal.weekly_cron(0, 9, 0) == "0 9 * * mon" and personal.weekly_cron(6, 9, 0) == "0 9 * * sun"
        now = datetime(2026, 11, 1, 12, 0)  # воскресенье
        assert rm._next_cron(personal.weekly_cron(0, 9, 0), "UTC", now) == datetime(2026, 11, 2, 9, 0)  # понедельник
        assert rm._next_cron(personal.weekly_cron(6, 9, 0), "UTC", now) == datetime(2026, 11, 8, 9, 0)  # воскресенье


class TestPersonalFlow:
    @pytest.mark.asyncio
    async def test_reminder_arrives_as_plain_text_to_the_author(self, sm):
        r = await personal.add_reminder(ME, "Позвонить <маме>", tz=TZ, run_at=soon(5))
        out = Outbox()
        assert await rm.run_once(out, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=6)) == 1
        assert [c for c, _ in out.sent] == [ME]
        assert "Позвонить &lt;маме&gt;" in out.sent[0][1]
        assert [x.id for x in await personal.list_reminders(ME)] == []  # разовое выполнено
        assert r.cron is None

    @pytest.mark.asyncio
    async def test_list_merges_spaces_and_hides_others_private_items(self, sm):
        fam = await service.create_space("Ивановы", DAD, kind="family")
        await service.add_member(fam.id, ME)
        await service.add_member(fam.id, OTHER)
        dad, other = await repo.open_scope(fam.id, DAD), await repo.open_scope(fam.id, OTHER)
        kw = dict(tz=TZ, run_at=soon(30))
        await personal.add_reminder(ME, "моё личное", **kw)
        await rm.create(dad, "всем", **kw)
        await rm.create(dad, "мне", assignee_id=ME, **kw)
        await rm.create(dad, "брату", assignee_id=OTHER, **kw)
        await rm.create(other, "моё, но чужое автору", assignee_id=OTHER, **kw)
        texts = sorted(r.text for r in await personal.list_reminders(ME))
        assert texts == ["всем", "мне", "моё личное"]

    @pytest.mark.asyncio
    async def test_cancel_own_and_not_others(self, sm):
        fam = await service.create_space("Ивановы", DAD, kind="family")
        await service.add_member(fam.id, ME)
        dad = await repo.open_scope(fam.id, DAD)
        theirs = await rm.create(dad, "папино", tz=TZ, run_at=soon())
        mine = await personal.add_reminder(ME, "моё", tz=TZ, run_at=soon())
        assert await personal.cancel(ME, theirs.id) is False  # рядовой участник чужое не отменяет
        assert await personal.cancel(ME, mine.id) is True
        assert await personal.cancel(ME, mine.id) is False
        assert await personal.cancel(OTHER, theirs.id) is False  # посторонний
        assert [r.text for r in await rm.list_active(dad)] == ["папино"]

    @pytest.mark.asyncio
    async def test_users_are_isolated(self, sm):
        await personal.add_reminder(ME, "моё", tz=TZ, run_at=soon())
        assert await personal.list_reminders(OTHER) == []
        out = Outbox()
        await rm.run_once(out, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))
        assert [c for c, _ in out.sent] == [ME]


def msg(uid=ME):
    m = MagicMock()
    m.from_user = SimpleNamespace(id=uid)
    m.answer = AsyncMock()
    m.text = ""
    return m


def state(**data):
    s = MagicMock()
    s.get_data = AsyncMock(return_value=data)
    s.clear = AsyncMock()
    return s


def said(m):
    return "\n".join(c.args[0] for c in m.answer.await_args_list)


class TestHandlers:
    @pytest.fixture(autouse=True)
    def _allowed(self, monkeypatch):
        monkeypatch.setattr(h, "user_allowed", AsyncMock(return_value=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind,text,check", [
        ("one_time", "через 30 минут", lambda r: r.cron is None),
        ("daily", "завтра в 9:30", lambda r: r.cron == "30 9 * * *"),
        ("weekly", "завтра в 9:30", lambda r: r.cron.startswith("30 9 * * ") and r.cron.split()[-1] in personal.DAYS),
    ])
    async def test_creation_goes_to_new_scheme(self, sm, kind, text, check):
        m = msg()
        m.text = text
        st = state(description="Принять таблетку", reminder_type=kind)
        await h.process_reminder_time(m, st)
        [r] = await personal.list_reminders(ME)
        assert r.text == "Принять таблетку" and check(r) and r.tz == TZ
        assert f"/cancel_task r{r.id}" in said(m)
        st.clear.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unparsable_time_and_past_time(self, sm):
        m = msg()
        m.text = "когда-нибудь"
        await h.process_reminder_time(m, state(description="x", reminder_type="one_time"))
        assert "Не удалось распознать" in said(m)
        m2 = msg()
        m2.text = "2020-01-01 10:00"
        st = state(description="x", reminder_type="one_time")
        await h.process_reminder_time(m2, st)
        assert "уже прошло" in said(m2) and await personal.list_reminders(ME) == []

    @pytest.mark.asyncio
    async def test_tasks_lists_new_and_legacy(self, sm, monkeypatch):
        await personal.add_reminder(ME, "новое <b>", tz=TZ, run_at=soon())
        legacy = SimpleNamespace(id=42, name="Старое", description="старая задача", task_type="one_time",
                                 next_run_at=datetime(2030, 1, 1, 10, 0), run_date=None)
        monkeypatch.setattr(h.task_scheduler, "get_user_tasks", AsyncMock(return_value=[legacy]))
        m = msg()
        await h.cmd_tasks(m)
        out = said(m)
        assert "новое &lt;b&gt;" in out and "/cancel_task r" in out and "/cancel_task 42" in out

    @pytest.mark.asyncio
    async def test_cancel_task_routes_by_prefix(self, sm, monkeypatch):
        legacy = AsyncMock(return_value=True)
        monkeypatch.setattr(h.task_scheduler, "cancel_task", legacy)
        r = await personal.add_reminder(ME, "x", tz=TZ, run_at=soon())
        m = msg()
        m.text = f"/cancel_task r{r.id}"
        await h.cmd_cancel_task(m)
        assert "отменена" in said(m) and await personal.list_reminders(ME) == []
        legacy.assert_not_awaited()
        m2 = msg()
        m2.text = "/cancel_task 42"
        await h.cmd_cancel_task(m2)
        legacy.assert_awaited_once_with(42, ME)
        for bad in ("/cancel_task rx", "/cancel_task <b>"):
            m3 = msg()
            m3.text = bad
            await h.cmd_cancel_task(m3)
            assert "Неверный ID" in said(m3)


class TestSchedulerReminderType:
    """Тип «reminder» раньше не планировался вовсе (молча не срабатывал)."""

    def _sched(self):
        from app.core.scheduler import TaskScheduler

        s = TaskScheduler()
        s.scheduler = MagicMock()
        s.scheduler.get_job.return_value = None
        return s

    @pytest.mark.asyncio
    async def test_one_time_reminder_is_scheduled(self):
        s = self._sched()
        task = SimpleNamespace(id=5, task_type="reminder", run_date=datetime.now() + timedelta(hours=1),
                               cron_expression=None, timezone="UTC", extra_data=None)
        await s._schedule_task(task)
        kw = s.scheduler.add_job.call_args.kwargs
        assert kw["id"] == "task_5" and type(kw["trigger"]).__name__ == "DateTrigger"

    @pytest.mark.asyncio
    async def test_cron_reminder_is_scheduled(self):
        s = self._sched()
        task = SimpleNamespace(id=6, task_type="reminder", run_date=None, cron_expression="0 9 * * *",
                               timezone="UTC", extra_data=None)
        await s._schedule_task(task)
        assert type(s.scheduler.add_job.call_args.kwargs["trigger"]).__name__ == "CronTrigger"

    @pytest.mark.asyncio
    async def test_other_types_unchanged(self):
        s = self._sched()
        await s._schedule_task(SimpleNamespace(id=7, task_type="recurring", run_date=None,
                                               cron_expression="0 3 * * *", timezone="UTC", extra_data=None))
        assert type(s.scheduler.add_job.call_args.kwargs["trigger"]).__name__ == "CronTrigger"
        s.scheduler.add_job.reset_mock()
        await s._schedule_task(SimpleNamespace(id=8, task_type="interval", run_date=None, cron_expression=None,
                                               timezone="UTC", extra_data={"interval_minutes": 10}))
        assert type(s.scheduler.add_job.call_args.kwargs["trigger"]).__name__ == "IntervalTrigger"
