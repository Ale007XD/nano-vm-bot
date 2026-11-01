"""Пространства в основном боте: создание, приглашение по ссылке, участники.

Создаёт пространство администратор бота (ADMIN_IDS): /space_new <вид> <название>.
Приглашает владелец или админ пространства: /invite [admin|member] [раз] [дней] — ссылка
t.me/<бот>?start=sp_<код>. Переход по ссылке работает и для тех, кого бот ещё не пускает
(это и есть способ попасть в закрытый бот), поэтому роутер стоит перед commands.router.
Команды работают с первым пространством пользователя (по дате вступления).
"""
import html
import logging
import re
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.core.access import is_admin
from app.core.config import settings
from app.spaces import repo, service
from app.spaces.terms import terms_for

logger = logging.getLogger(__name__)

router = Router()

_CODE_RE = re.compile(r"^sp_([A-Za-z0-9_-]{6,32})$")
_ERRORS = {
    service.RESULT_INVALID: "Приглашение недействительно.",
    service.RESULT_REVOKED: "Приглашение отозвано.",
    service.RESULT_EXPIRED: "Срок приглашения истёк.",
    service.RESULT_EXHAUSTED: "Приглашение уже использовано.",
    service.RESULT_FULL: "В пространстве нет мест.",
}


def _enabled() -> bool:
    return getattr(settings, "SPACES_ENABLED", False) is True


async def _is_banned(user_id: int) -> bool:
    try:
        from app.core.database import is_user_banned

        return bool(await is_user_banned(user_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ban check failed for {user_id}: {e}")
        return False


def _display(message: Message) -> str:
    u = message.from_user
    return (u.first_name or u.username or str(u.id)) if u else ""


async def _scope(message: Message, needed: str = "member") -> Optional[repo.SpaceScope]:
    """Scope пространства пользователя с проверкой роли; сам отвечает пользователю при отказе."""
    scope = await repo.scope_for_user(message.from_user.id)
    if scope is None:
        await message.answer("Вы не состоите ни в одном пространстве.")
        return None
    if not scope.can(needed):
        await message.answer("⛔ Нужны права администратора пространства.")
        return None
    return scope


# Глубокая ссылка: CommandStart(deep_link=True) с фильтром по формату аргумента.
@router.message(CommandStart(deep_link=True, magic=F.args.regexp(_CODE_RE)), F.chat.type == "private")
async def cmd_start_join(message: Message, command: CommandObject):
    if not _enabled():
        await message.answer("Приглашения в пространства сейчас отключены.")
        return
    uid = message.from_user.id
    if await _is_banned(uid):
        await message.answer("⛔ Доступ запрещён.")
        return
    code = _CODE_RE.match(command.args or "").group(1)
    result, space_id = await service.redeem_invite(code, uid, display_name=_display(message))
    if result not in (service.RESULT_OK, service.RESULT_ALREADY):
        await message.answer(_ERRORS.get(result, "Приглашение недействительно.") + "\n\nЗапросите новую ссылку.")
        return
    space = await service.get_space(space_id)
    t = terms_for(space.kind, space.settings)
    head = "✅ Вы присоединились" if result == service.RESULT_OK else "Вы уже в пространстве"
    await message.answer(
        f"{head}: <b>{html.escape(t['space'])} «{html.escape(space.name)}»</b>.\n\n"
        "Дальше просто пишите или наговаривайте голосом. Участники: /members"
    )


@router.message(Command("space_new"), F.chat.type == "private")
async def cmd_space_new(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администраторов бота.")
        return
    if not _enabled():
        await message.answer("Включите SPACES_ENABLED=true в .env и перезапустите бота.")
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: <code>/space_new &lt;вид&gt; &lt;название&gt;</code>\nНапример: <code>/space_new family Ивановы</code>")
        return
    kind, name = parts[0].lower(), parts[1]
    try:
        space = await service.create_space(
            name, message.from_user.id, kind=kind, timezone_name=settings.BOT_TIMEZONE,
            owner_name=_display(message),
        )
    except service.SpaceError as e:
        await message.answer(f"❌ {html.escape(str(e))}")
        return
    await message.answer(
        f"✅ Создано: <b>{html.escape(space.name)}</b> (вид: {html.escape(space.kind)}, id {space.id}).\n"
        "Пригласить участников: /invite"
    )


@router.message(Command("invite"), F.chat.type == "private")
async def cmd_invite(message: Message, command: CommandObject):
    if not _enabled():
        return
    scope = await _scope(message, "admin")
    if scope is None:
        return
    args = (command.args or "").split()
    role = "member"
    if args and args[0] in ("admin", "member"):
        role = args.pop(0)
    try:
        if len(args) > 2:
            raise ValueError
        uses = int(args[0]) if args else 1
        days = int(args[1]) if len(args) > 1 else None
        if uses < 0 or (days is not None and days < 1):
            raise ValueError
    except ValueError:
        await message.answer("Формат: <code>/invite [admin|member] [сколько раз] [дней]</code>")
        return
    code = await service.create_invite(scope.space_id, message.from_user.id, role=role, max_uses=uses, days=days)
    me = await message.bot.me()
    await message.answer(
        "🔗 <b>Приглашение</b>\n"
        f"https://t.me/{me.username}?start=sp_{code}\n\n"
        f"Роль: {role}, использований: {uses or 'без ограничения'}, "
        f"срок: {days or service.invite_days()} дн.\n"
        f"Отозвать: <code>/revoke_invite {code}</code>"
    )


@router.message(Command("revoke_invite"), F.chat.type == "private")
async def cmd_revoke_invite(message: Message, command: CommandObject):
    if not _enabled():
        return
    scope = await _scope(message, "admin")
    if scope is None:
        return
    code = (command.args or "").strip()
    ok = bool(code) and await service.revoke_invite(scope.space_id, code)
    await message.answer("✅ Приглашение отозвано." if ok else "Приглашение не найдено в вашем пространстве.")


@router.message(Command("members"), F.chat.type == "private")
async def cmd_members(message: Message):
    if not _enabled():
        return
    scope = await _scope(message)
    if scope is None:
        return
    space = await service.get_space(scope.space_id)
    t = terms_for(space.kind, space.settings)
    rows = await service.list_members(scope.space_id)
    lines = [f"👥 <b>{html.escape(t['members'])}</b> — {html.escape(space.name)} ({len(rows)}/{service.max_members()})"]
    for m in rows:
        name = html.escape(m.display_name or str(m.user_id))
        lines.append(f"• {name} — {m.role}" + (f" (<code>{m.user_id}</code>)" if scope.can("admin") else ""))
    await message.answer("\n".join(lines))


@router.message(Command("kick"), F.chat.type == "private")
async def cmd_kick(message: Message, command: CommandObject):
    if not _enabled():
        return
    scope = await _scope(message, "admin")
    if scope is None:
        return
    try:
        target = int((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: <code>/kick &lt;id&gt;</code> (id — в списке /members)")
        return
    target_role = await service.member_role(scope.space_id, target)
    if target_role is None:
        await message.answer("Такого участника нет.")
        return
    if service.ROLE_RANK_OF(target_role) > service.ROLE_RANK_OF(scope.role):
        await message.answer("⛔ Нельзя убрать участника со старшей ролью.")
        return
    try:
        await service.remove_member(scope.space_id, target)
    except service.SpaceError as e:
        await message.answer(f"❌ {html.escape(str(e))}")
        return
    await message.answer("✅ Участник убран.")


@router.message(Command("leave"), F.chat.type == "private")
async def cmd_leave(message: Message):
    if not _enabled():
        return
    scope = await _scope(message)
    if scope is None:
        return
    try:
        await service.remove_member(scope.space_id, message.from_user.id)
    except service.SpaceError as e:
        await message.answer(f"❌ {html.escape(str(e))}")
        return
    await message.answer("Вы вышли из пространства.")
