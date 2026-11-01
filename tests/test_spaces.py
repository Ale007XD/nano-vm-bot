"""Пространства: создание, роли, приглашения, лимит участников, изоляция данных, доступ, подписи."""
import asyncio
import sys
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import Boolean, Column, Integer, String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.core.ext_db import ExtBase
from app.spaces import repo, service
from app.spaces.models import SpaceInvite, SpaceOwned
from app.spaces.terms import terms_for

OWNER, MOM, KID, STRANGER = 101, 102, 103, 999

DataBase = declarative_base()


class Item(SpaceOwned, DataBase):  # таблица данных пространства для проверки изоляции
    __tablename__ = "test_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(String(100), nullable=False)
    done = Column(Boolean, default=False)


class Loose(DataBase):  # без space_id — scope должен отказать
    __tablename__ = "test_loose"
    id = Column(Integer, primary_key=True)


@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
        await conn.run_sync(DataBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (service, repo):
        monkeypatch.setattr(mod, "resolve_session_maker", lambda _=None, m=maker: m)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    st = sys.modules["app.core.config"].settings
    for k, v in dict(SPACE_MAX_MEMBERS=3, SPACE_INVITE_TTL_DAYS=7, SPACES_ENABLED=True,
                     ADMIN_IDS=[1], BOT_OPEN_ACCESS=False).items():
        monkeypatch.setattr(st, k, v, raising=False)
    return st


async def family(name="Ивановы"):
    return await service.create_space(name, OWNER, owner_name="Папа")


class TestSpace:
    @pytest.mark.asyncio
    async def test_create_adds_owner(self, sm):
        sp = await family()
        assert await service.member_role(sp.id, OWNER) == "owner"
        assert (sp.kind, sp.currency, sp.timezone) == ("family", "RUB", "UTC")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name,kind", [("  ", "family"), ("x", "Bad Kind"), ("x", "")])
    async def test_validation(self, sm, name, kind):
        with pytest.raises(service.SpaceError):
            await service.create_space(name, OWNER, kind=kind)

    @pytest.mark.asyncio
    async def test_settings_merge(self, sm):
        sp = await service.create_space("A", OWNER, settings={"report_day": 1})
        await service.update_settings(sp.id, {"terms": {"member": "родич"}})
        got = await service.get_space(sp.id)
        assert got.settings == {"report_day": 1, "terms": {"member": "родич"}}

    @pytest.mark.asyncio
    async def test_deactivated_space_denies_everything(self, sm):
        sp = await family()
        await service.deactivate_space(sp.id)
        assert await service.get_space(sp.id) is None
        assert await service.member_role(sp.id, OWNER) is None
        assert not await service.is_member_anywhere(OWNER)
        assert await service.spaces_of(OWNER) == []
        with pytest.raises(repo.SpaceAccessError):
            await repo.open_scope(sp.id, OWNER)
        assert await service.add_member(sp.id, MOM) == service.RESULT_INVALID


class TestMembers:
    @pytest.mark.asyncio
    async def test_add_duplicate_and_limit(self, sm):
        sp = await family()
        assert await service.add_member(sp.id, MOM) == service.RESULT_OK
        assert await service.add_member(sp.id, MOM) == service.RESULT_ALREADY
        assert await service.add_member(sp.id, KID) == service.RESULT_OK
        assert await service.add_member(sp.id, STRANGER) == service.RESULT_FULL  # лимит 3
        assert len(await service.list_members(sp.id)) == 3

    @pytest.mark.asyncio
    async def test_last_owner_is_protected(self, sm):
        sp = await family()
        with pytest.raises(service.SpaceError):
            await service.remove_member(sp.id, OWNER)
        with pytest.raises(service.SpaceError):
            await service.set_role(sp.id, OWNER, "member")
        await service.add_member(sp.id, MOM, role="owner")
        assert await service.set_role(sp.id, OWNER, "member")  # теперь есть второй владелец
        assert await service.remove_member(sp.id, OWNER)

    @pytest.mark.asyncio
    async def test_bad_role_rejected(self, sm):
        sp = await family()
        with pytest.raises(service.SpaceError):
            await service.add_member(sp.id, MOM, role="root")
        with pytest.raises(service.SpaceError):
            await service.set_role(sp.id, OWNER, "root")

    @pytest.mark.asyncio
    async def test_role_order(self):
        assert service.role_at_least("owner", "admin") and service.role_at_least("admin", "admin")
        assert not service.role_at_least("member", "admin") and not service.role_at_least(None, "member")


class TestInvites:
    @pytest.mark.asyncio
    async def test_redeem_flow(self, sm):
        sp = await family()
        code = await service.create_invite(sp.id, OWNER)
        assert await service.redeem_invite(code, MOM, display_name="Мама") == (service.RESULT_OK, sp.id)
        assert await service.member_role(sp.id, MOM) == "member"
        # одноразовый: второй человек не пройдёт, а повтор участника не расходует ничего
        assert await service.redeem_invite(code, KID) == (service.RESULT_EXHAUSTED, sp.id)
        assert await service.redeem_invite(code, MOM) == (service.RESULT_ALREADY, sp.id)

    @pytest.mark.asyncio
    async def test_invalid_revoked_expired(self, sm):
        sp = await family()
        assert await service.redeem_invite("nope", MOM) == (service.RESULT_INVALID, None)
        code = await service.create_invite(sp.id, OWNER, max_uses=5)
        await service.revoke_invite(sp.id, code)
        assert (await service.redeem_invite(code, MOM))[0] == service.RESULT_REVOKED
        code2 = await service.create_invite(sp.id, OWNER, max_uses=5)
        async with sm() as s:
            inv = await s.get(SpaceInvite, code2)
            inv.expires_at = inv.expires_at - timedelta(days=30)
            await s.commit()
        assert (await service.redeem_invite(code2, MOM))[0] == service.RESULT_EXPIRED
        assert not await service.is_member_anywhere(MOM)

    @pytest.mark.asyncio
    async def test_cannot_invite_owner(self, sm):
        sp = await family()
        with pytest.raises(service.SpaceError):
            await service.create_invite(sp.id, OWNER, role="owner")

    @pytest.mark.asyncio
    async def test_invite_role_and_full_space_does_not_burn_code(self, sm):
        sp = await family()
        await service.add_member(sp.id, MOM)
        await service.add_member(sp.id, KID)  # 3 из 3
        code = await service.create_invite(sp.id, OWNER, role="admin", max_uses=1)
        assert (await service.redeem_invite(code, STRANGER))[0] == service.RESULT_FULL
        async with sm() as s:
            assert (await s.get(SpaceInvite, code)).uses == 0
        await service.remove_member(sp.id, KID)
        assert (await service.redeem_invite(code, STRANGER))[0] == service.RESULT_OK
        assert await service.member_role(sp.id, STRANGER) == "admin"

    @pytest.mark.asyncio
    async def test_invite_of_deactivated_space_invalid(self, sm):
        sp = await family()
        code = await service.create_invite(sp.id, OWNER)
        await service.deactivate_space(sp.id)
        assert await service.redeem_invite(code, MOM) == (service.RESULT_INVALID, None)

    @pytest.mark.asyncio
    async def test_parallel_redeem_respects_max_uses(self, tmp_path, monkeypatch):
        # файловая БД: у каждой сессии своё соединение (в :memory: оно общее, и rollback одной
        # сессии откатывал бы UPDATE другой — это не воспроизводит боевое поведение)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(ExtBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(service, "resolve_session_maker", lambda _=None: maker)
        sp = await service.create_space("Большая", OWNER)
        code = await service.create_invite(sp.id, OWNER, max_uses=1)
        results = await asyncio.gather(*(service.redeem_invite(code, 500 + i) for i in range(4)))
        await engine.dispose()
        assert [r[0] for r in results].count(service.RESULT_OK) == 1
        assert sorted(r[0] for r in results if r[0] != service.RESULT_OK) == [service.RESULT_EXHAUSTED] * 3


class TestIsolation:
    """Главное свойство: данные одного пространства не видны и не меняются из другого."""

    @pytest.mark.asyncio
    async def test_scope_requires_membership(self, sm):
        sp = await family()
        with pytest.raises(repo.SpaceAccessError):
            await repo.open_scope(sp.id, STRANGER)
        assert await repo.scope_for_user(STRANGER) is None

    @pytest.mark.asyncio
    async def test_no_leak_between_spaces(self, sm):
        a = await service.create_space("A", OWNER)
        b = await service.create_space("B", MOM)
        sa, sb = await repo.open_scope(a.id, OWNER), await repo.open_scope(b.id, MOM)
        ia = await sa.add(Item(text="молоко"))
        ib = await sb.add(Item(text="секрет B"))

        assert [i.text for i in await sa.list(Item)] == ["молоко"]
        assert [i.text for i in await sb.list(Item)] == ["секрет B"]
        assert await sa.count(Item) == 1
        assert await sa.get(Item, ib.id) is None              # чужая запись по прямому id
        assert not await sa.update(Item, ib.id, text="взлом")  # не меняется
        assert not await sa.delete(Item, ib.id)                # не удаляется
        assert (await sb.get(Item, ib.id)).text == "секрет B"
        assert (await sa.get(Item, ia.id)).text == "молоко"

    @pytest.mark.asyncio
    async def test_add_cannot_target_other_space(self, sm):
        a = await service.create_space("A", OWNER)
        b = await service.create_space("B", MOM)
        sa = await repo.open_scope(a.id, OWNER)
        with pytest.raises(repo.SpaceAccessError):
            await sa.add(Item(text="подброс", space_id=b.id))
        with pytest.raises(repo.SpaceAccessError):
            await sa.update(Item, 1, space_id=b.id)

    @pytest.mark.asyncio
    async def test_filters_compose_and_model_without_space_id_rejected(self, sm):
        a = await service.create_space("A", OWNER)
        sa = await repo.open_scope(a.id, OWNER)
        await sa.add(Item(text="1", done=True))
        await sa.add(Item(text="2", done=False))
        assert [i.text for i in await sa.list(Item, Item.done.is_(False))] == ["2"]
        assert await sa.count(Item, Item.done.is_(True)) == 1
        assert [i.text for i in await sa.list(Item, order_by=Item.text.desc(), limit=1)] == ["2"]
        with pytest.raises(TypeError):
            await sa.list(Loose)

    @pytest.mark.asyncio
    async def test_membership_revoked_stops_scope(self, sm):
        a = await service.create_space("A", OWNER)
        await service.add_member(a.id, MOM)
        await service.remove_member(a.id, MOM)
        with pytest.raises(repo.SpaceAccessError):
            await repo.open_scope(a.id, MOM)

    @pytest.mark.asyncio
    async def test_scope_role_checks(self, sm):
        a = await service.create_space("A", OWNER)
        await service.add_member(a.id, KID)
        kid = await repo.open_scope(a.id, KID)
        assert kid.can("member") and not kid.can("admin")
        with pytest.raises(repo.SpaceAccessError):
            kid.require("admin")


class TestAccessGate:
    @pytest.fixture
    def banned(self, monkeypatch):
        db = sys.modules["app.core.database"]
        mock = AsyncMock(return_value=False)
        monkeypatch.setattr(db, "is_user_banned", mock)
        return mock

    @pytest.mark.asyncio
    async def test_member_allowed_in_closed_bot(self, sm, banned):
        from app.core.access import user_allowed

        await family()
        assert await user_allowed(OWNER)
        assert not await user_allowed(STRANGER)
        assert await user_allowed(1)  # админ

    @pytest.mark.asyncio
    async def test_flag_off_means_no_effect(self, sm, banned, cfg, monkeypatch):
        from app.core.access import user_allowed

        await family()
        monkeypatch.setattr(cfg, "SPACES_ENABLED", False)
        assert not await user_allowed(OWNER)

    @pytest.mark.asyncio
    async def test_banned_member_denied(self, sm, banned):
        from app.core.access import user_allowed

        await family()
        banned.return_value = True
        assert not await user_allowed(OWNER)

    @pytest.mark.asyncio
    async def test_showcase_override_ignores_spaces(self, sm, banned):
        from app.core.access import user_allowed

        await family()
        assert not await user_allowed(OWNER, open_access=False)  # закрытая витрина — не для семьи

    @pytest.mark.asyncio
    async def test_db_failure_keeps_closed(self, sm, banned, monkeypatch):
        from app.core.access import user_allowed

        monkeypatch.setattr(service, "is_member_anywhere", AsyncMock(side_effect=RuntimeError("db")))
        assert not await user_allowed(OWNER)

    @pytest.mark.asyncio
    async def test_deactivated_space_closes_gate(self, sm, banned):
        from app.core.access import user_allowed

        sp = await family()
        await service.deactivate_space(sp.id)
        assert not await user_allowed(OWNER)


class TestTerms:
    def test_kinds_and_fallback(self):
        assert terms_for("family")["member"] == "член семьи"
        assert terms_for("class")["ledger"] == "Взносы"
        assert terms_for("unknown")["space"] == "Группа"
        assert terms_for(None)["space"] == "Группа"

    def test_settings_override_ignores_junk(self):
        t = terms_for("family", {"terms": {"member": "родич", "space": "  ", "task": 5}})
        assert t["member"] == "родич" and t["space"] == "Семья" and t["task"] == "Дело"

    def test_all_kinds_have_same_keys(self):
        from app.spaces.terms import TERMS

        keys = set(TERMS["generic"])
        assert all(set(v) == keys for v in TERMS.values())

    def test_no_domain_words_in_code(self):
        import pathlib

        for f in pathlib.Path("app/spaces").glob("*.py"):
            if f.name not in ("terms.py", "__init__.py"):
                assert "семь" not in f.read_text(encoding="utf-8").lower(), f.name


class TestRegistered:
    def test_models_are_registered_in_ext_db(self):
        from app.core import ext_db

        assert "app.spaces.models" in ext_db._FEATURE_MODULES
