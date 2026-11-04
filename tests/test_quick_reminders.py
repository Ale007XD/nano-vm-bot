"""Напоминания из свободного текста/голоса: настоящее напоминание вместо выдуманного ответа агента."""
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.ext_db import ExtBase
from app.spaces import intake, personal, quick, repo, service
from app.spaces import reminders as rm

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
from app.handlers import messages as hm  # noqa: E402

ME = 7
TZ = "Asia/Ho_Chi_Minh"
NOW = datetime(2027, 3, 3, 5, 27, tzinfo=timezone.utc)  # 12:27 по Хошимину, среда; в будущем, т.к. create() сверяет с реальными часами


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
    for k, v in dict(SPACES_ENABLED=True, SPACE_MAX_MEMBERS=10, SPACE_INVITE_TTL_DAYS=7, BOT_TIMEZONE=TZ).items():
        monkeypatch.setattr(st, k, v, raising=False)


def llm_says(obj):
    seen = {}

    async def llm(system, user):
        seen["system"], seen["user"] = system, user
        return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    llm.seen = seen
    return llm


def item(**kw):
    base = {"type": "reminder", "text": "позвонить", "at": None, "repeat": "none", "weekday": None,
            "day": None, "time": None, "for": None}
    base.update(kw)
    return {"items": [base]}


class TestTrigger:
    @pytest.mark.parametrize("t", ["Оповещение в этот чат через 5 минут. Повтор.", "напомни завтра в 18:00 купить хлеб",
                                   "Поставь будильник на 7", "уведоми меня в пятницу", "таймер на 10 минут", "напоминай каждый день в 8",
                                   "Напоминание на завтра"])
    def test_yes(self, t):
        assert quick.looks_like_reminder(t)

    @pytest.mark.parametrize("t", ["", None, "   ", "привет, как дела", "через 5 минут буду",
                                   "напомни https://example.com/post", "напомни " + "а" * quick.MAX_LEN])
    def test_no(self, t):
        assert not quick.looks_like_reminder(t)


class TestHandle:
    @pytest.mark.asyncio
    async def test_relative_time_creates_real_reminder(self, sm):
        llm = llm_says(item(text="Напоминание", at="2027-03-03T12:32"))
        out = await quick.handle("Оповещение через 5 минут", ME, llm, now=NOW)
        assert "⏰" in out and "/cancel_task r" in out
        rs = await personal.list_reminders(ME, TZ)
        assert len(rs) == 1 and rs[0].text == "Напоминание"
        assert rs[0].next_run_at == datetime(2027, 3, 3, 5, 32)  # 12:32 по Хошимину в UTC
        assert "12:32" in llm.seen["system"] or "2027-03-03 12:27" in llm.seen["system"]

    @pytest.mark.asyncio
    async def test_weekday(self, sm):
        out = await quick.handle("напомни в пятницу в 9:00 отчёт", ME,
                                 llm_says(item(text="отчёт", weekday="fri", time="09:00")), now=NOW)
        assert "⏰" in out
        assert (await personal.list_reminders(ME, TZ))[0].next_run_at == datetime(2027, 3, 5, 2, 0)

    @pytest.mark.asyncio
    async def test_recurring(self, sm):
        out = await quick.handle("напоминай каждый день в 8:00 про таблетки", ME,
                                 llm_says(item(text="таблетки", repeat="daily", time="08:00")), now=NOW)
        assert "⏰" in out
        r = (await personal.list_reminders(ME, TZ))[0]
        assert r.cron == "0 8 * * *" and (r.next_run_at.hour, r.next_run_at.minute) == (1, 0)  # 08:00 по Хошимину

    @pytest.mark.asyncio
    async def test_not_trigger_skips_llm(self, sm):
        llm = AsyncMock()
        assert await quick.handle("привет", ME, llm, now=NOW) is None
        llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_answer_falls_through(self, sm):
        assert await quick.handle("напомни что-нибудь", ME, llm_says({"items": []}), now=NOW) is None
        assert await personal.list_reminders(ME, TZ) == []

    @pytest.mark.asyncio
    async def test_no_time_gives_notes_not_reminder(self, sm):
        out = await quick.handle("напомни купить хлеб", ME, llm_says(item(text="купить хлеб")), now=NOW)
        assert out and "⏰" not in out and quick.HELP in out
        assert await personal.list_reminders(ME, TZ) == []

    @pytest.mark.asyncio
    async def test_other_types_ignored(self, sm):
        raw = {"items": [{"type": "expense", "amount": 500, "text": "кофе"}]}
        assert await quick.handle("напомни про кофе", ME, llm_says(raw), now=NOW) is None
        assert await personal.list_reminders(ME, TZ) == []

    @pytest.mark.asyncio
    async def test_past_time_rejected(self, sm):
        out = await quick.handle("напомни вчера в 10:00 позвонить", ME,
                                 llm_says(item(at="2027-03-02T10:00")), now=NOW)
        assert out and "⏰" not in out
        assert await personal.list_reminders(ME, TZ) == []

    @pytest.mark.asyncio
    async def test_llm_down_is_honest(self, sm):
        async def boom(s, u):
            raise RuntimeError("429")
        out = await quick.handle("напомни завтра в 9 позвонить", ME, boom, now=NOW)
        assert "Не получилось" in out and "⏰" not in out

    @pytest.mark.asyncio
    async def test_garbage_json(self, sm):
        out = await quick.handle("напомни завтра в 9 позвонить", ME, llm_says("ой"), now=NOW)
        assert "Не смог" in out

    @pytest.mark.asyncio
    async def test_group_note(self, sm):
        llm = llm_says(item(at="2027-03-03T13:00"))
        assert "личные сообщения" in await quick.handle("напомни в 13:00", ME, llm, in_group=True, now=NOW)
        assert "личные" not in await quick.handle("напомни в 13:00", ME, llm, now=NOW)

    @pytest.mark.asyncio
    async def test_html_escaped(self, sm):
        out = await quick.handle("напомни в 13:00", ME, llm_says(item(text="<b>x</b>", at="2027-03-03T13:00")), now=NOW)
        assert "&lt;b&gt;x&lt;/b&gt;" in out

    def test_prompt_is_reminder_only(self):
        ctx = intake.Context(now_local=NOW, tz=TZ, currency="RUB", kind="personal", members={}, categories=[],
                             only=("reminder",))
        p = intake.build_system_prompt(ctx)
        assert "type\":\"shopping" not in p and "type\":\"expense" not in p and "2027-03-03" in p


def msg(text="напомни через 5 минут", chat="private"):
    return SimpleNamespace(from_user=SimpleNamespace(id=ME), chat=SimpleNamespace(type=chat),
                           answer=AsyncMock(), text=text)


class TestHook:
    @pytest.mark.asyncio
    async def test_handled(self, monkeypatch):
        h = AsyncMock(return_value="OK")
        monkeypatch.setattr(quick, "handle", h)
        m = msg()
        assert await hm._quick_reminder(m, m.text) is True
        m.answer.assert_awaited_once_with("OK")
        assert h.await_args.kwargs["in_group"] is False

    @pytest.mark.asyncio
    async def test_group_flag(self, monkeypatch):
        h = AsyncMock(return_value="OK")
        monkeypatch.setattr(quick, "handle", h)
        await hm._quick_reminder(msg(chat="supergroup"), "напомни завтра")
        assert h.await_args.kwargs["in_group"] is True

    @pytest.mark.asyncio
    async def test_falls_through(self, monkeypatch):
        monkeypatch.setattr(quick, "handle", AsyncMock(return_value=None))
        m = msg()
        assert await hm._quick_reminder(m, m.text) is False
        m.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plain_text_never_calls_quick(self, monkeypatch):
        h = AsyncMock()
        monkeypatch.setattr(quick, "handle", h)
        assert await hm._quick_reminder(msg("привет"), "привет") is False
        h.assert_not_awaited()

    def test_wired_into_text_and_voice(self):
        import inspect
        assert "_quick_reminder(message, user_message)" in inspect.getsource(hm.handle_message)
        assert "_quick_reminder(message, transcribed_text)" in inspect.getsource(hm.handle_voice)


def test_datetime_prompt_forbids_promises():
    from app.utils.helpers import current_datetime_prompt
    p = current_datetime_prompt("UTC")
    assert "не обещай" in p and "/remind" in p


@pytest.mark.asyncio
async def test_named_weekday_beats_wrong_model_date(sm):
    """Среда 03.03, «в пятницу в 9», модель дала среду → ставим пятницу 05.03 09:00 (02:00 UTC)."""
    llm = llm_says(item(text="отчёт", at="2027-03-03T09:00"))
    await quick.handle("напомни в пятницу в 9 отчёт", ME, llm, now=NOW)
    assert (await personal.list_reminders(ME, TZ))[0].next_run_at == datetime(2027, 3, 5, 2, 0)
