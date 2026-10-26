"""
Админ-гейт на создание/правку/удаление/просмотр кода навыков
(app/handlers/skills.py) и валидация имени в SkillLoader.create_skill.

Создание .py-навыка = исполнение кода в процессе бота, поэтому любой
не-админ (в т.ч. при BOT_OPEN_ACCESS=true) обязан получать отказ.
"""
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

ADMIN_ID = 1
USER_ID = 999


@pytest.fixture(autouse=True)
def _admins(monkeypatch):
    settings = sys.modules["app.core.config"].settings
    monkeypatch.setattr(settings, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(settings, "BOT_OPEN_ACCESS", True)
    # импорт app.handlers тянет планировщик, ему нужна валидная таймзона
    monkeypatch.setattr(settings, "BOT_TIMEZONE", "UTC")


@pytest.fixture
def skills_mod(monkeypatch):
    from app.handlers import skills

    loader = MagicMock()
    loader.create_skill = AsyncMock()
    loader.delete_skill = AsyncMock()
    loader.get_skill_code = AsyncMock(return_value="print(1)")
    loader.get_skill_info = MagicMock(return_value=MagicMock(source="custom", description="d"))
    monkeypatch.setattr(skills, "skill_loader", loader)
    return skills


def _cb(user_id, data):
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.data = data
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    return cb


def _msg(user_id, text="x"):
    m = MagicMock()
    m.from_user.id = user_id
    m.from_user.username = "u"
    m.text = text
    m.answer = AsyncMock()
    return m


def _state(data=None):
    st = MagicMock()
    st.clear = AsyncMock()
    st.set_state = AsyncMock()
    st.update_data = AsyncMock()
    st.get_data = AsyncMock(return_value=data or {})
    return st


CALLBACK_CASES = [
    ("skill_create_start", "skills:create", True),
    ("skill_code", "skill:code:foo", False),
    ("skill_edit_start", "skill:edit:foo", True),
    ("skill_delete_confirm", "skill:delete:foo", False),
    ("skill_delete", "skill:confirm_delete:foo", False),
]


def _call_cb(mod, func, cb, takes_state):
    fn = getattr(mod, func)
    return fn(cb, _state()) if takes_state else fn(cb)


@pytest.mark.parametrize("func,data,takes_state", CALLBACK_CASES)
@pytest.mark.asyncio
async def test_callbacks_deny_non_admin(skills_mod, func, data, takes_state):
    cb = _cb(USER_ID, data)
    await _call_cb(skills_mod, func, cb, takes_state)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_called()
    skills_mod.skill_loader.create_skill.assert_not_called()
    skills_mod.skill_loader.delete_skill.assert_not_called()


@pytest.mark.parametrize("func,data,takes_state", CALLBACK_CASES)
@pytest.mark.asyncio
async def test_callbacks_allow_admin(skills_mod, func, data, takes_state):
    cb = _cb(ADMIN_ID, data)
    await _call_cb(skills_mod, func, cb, takes_state)
    assert cb.message.edit_text.await_count >= 1


@pytest.mark.asyncio
async def test_confirm_delete_parses_name_and_deletes(skills_mod):
    """Регрессия: split(':')[3] давал IndexError, удаление не работало."""
    cb = _cb(ADMIN_ID, "skill:confirm_delete:my_skill")
    await skills_mod.skill_delete(cb)
    skills_mod.skill_loader.delete_skill.assert_awaited_once_with("my_skill")


FSM_CASES = [
    ("skill_create_name", {}),
    ("skill_create_description", {}),
    ("skill_create_code", {"skill_name": "evil", "skill_description": "d"}),
    ("skill_edit_save", {"skill_name": "evil"}),
]


@pytest.mark.parametrize("func,data", FSM_CASES)
@pytest.mark.asyncio
async def test_fsm_steps_deny_non_admin(skills_mod, func, data):
    msg, st = _msg(USER_ID, "import os"), _state(data)
    await getattr(skills_mod, func)(msg, st)

    st.clear.assert_awaited_once()
    st.set_state.assert_not_called()
    skills_mod.skill_loader.create_skill.assert_not_called()


@pytest.mark.asyncio
async def test_create_code_admin_creates_skill(skills_mod):
    msg = _msg(ADMIN_ID, "SKILL_NAME='x'")
    st = _state({"skill_name": "x", "skill_description": "d"})
    await skills_mod.skill_create_code(msg, st)
    skills_mod.skill_loader.create_skill.assert_awaited_once()


class TestCreateSkillNameValidation:
    @pytest.mark.parametrize("bad", ["../evil", "a/b", "a.b", "", "1abc", "a b", "..", "x\n"])
    @pytest.mark.asyncio
    async def test_rejects_invalid_names(self, bad, tmp_path, monkeypatch):
        from app.core import skills_loader as sl

        monkeypatch.setitem(sl._SKILL_DIRS, "custom", str(tmp_path))
        with pytest.raises(ValueError):
            await sl.SkillLoader().create_skill(bad, "d", "x = 1")
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_accepts_valid_name(self, tmp_path, monkeypatch):
        from app.core import skills_loader as sl

        monkeypatch.setitem(sl._SKILL_DIRS, "custom", str(tmp_path))
        await sl.SkillLoader().create_skill("good_name", "d", "x = 1")
        assert (tmp_path / "good_name.py").is_file()
