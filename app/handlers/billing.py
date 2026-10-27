"""
Покупка пакетов запросов (кредитов).

  /buy        — каталог пакетов и способы оплаты
  /balance    — баланс и последние платежи
  /paysupport — помощь по платежам (обязательна для ботов с оплатой в Telegram)
  /grant <id> <n>   (админ) — начислить кредиты
  /refund <платёж>  (админ) — вернуть платёж и снять кредиты

1 кредит = 1 сообщение сверх бесплатного суточного лимита (см. access.check_daily_limit).
"""
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.access import is_admin, user_allowed
from app.core.billing import service
from app.core.billing.packages import get_package, get_packages
from app.core.billing.providers import PaymentProvider, build_providers
from app.core.config import settings

logger = logging.getLogger(__name__)

router = Router()

_CURRENCY_FMT = {
    "XTR": lambda v: f"{v} ⭐",
    "RUB": lambda v: f"{v / 100:.0f} ₽",
    "USD": lambda v: f"${v / 100:.2f}",
}


def _fmt_price(currency: str, value: int) -> str:
    return _CURRENCY_FMT.get(currency, lambda v: f"{v} {currency}")(value)


def _providers():
    return build_providers(settings)


# ----------------------------- пользователь ------------------------------ #
@router.message(Command("buy"), F.chat.type == "private")
async def cmd_buy(message: Message):
    if not await user_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещен.")
        return
    providers = _providers()
    packages = get_packages()
    if not providers or not packages:
        await message.answer("Оплата сейчас недоступна.")
        return

    kb = InlineKeyboardBuilder()
    lines = ["💳 <b>Пакеты запросов</b>\n"]
    for pkg in packages:
        offers = [(p, pkg.price(p.currency)) for p in providers.values() if pkg.price(p.currency)]
        if not offers:
            continue
        lines.append(f"• <b>{html.escape(pkg.title)}</b> — {pkg.credits} сообщений")
        for prov, amount in offers:
            kb.button(
                text=f"{pkg.title}: {_fmt_price(prov.currency, amount)} · {prov.label}",
                callback_data=f"buy:{prov.name}:{pkg.id}",
            )
    kb.adjust(1)
    lines.append(
        "\nКредиты тратятся только после исчерпания бесплатного суточного лимита "
        "и не сгорают. Баланс — /balance, вопросы по платежам — /paysupport."
    )
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    await callback.answer()
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or callback.message is None:
        return
    _, prov_name, pkg_id = parts
    if not await user_allowed(callback.from_user.id):
        return
    prov = _providers().get(prov_name)
    pkg = get_package(pkg_id)
    if prov is None or pkg is None or pkg.price(prov.currency) is None:
        await callback.message.answer("Этот вариант оплаты больше недоступен. /buy")
        return
    try:
        await prov.send_invoice(callback.bot, callback.message.chat.id, callback.from_user.id, pkg)
    except Exception as e:
        logger.error("send_invoice failed (%s/%s): %s", prov_name, pkg_id, e)
        await callback.message.answer("⚠️ Не удалось создать счёт. Попробуйте позже.")


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Подтвердить счёт, только если он сверяется с нашим каталогом (ответить надо за 10 с)."""
    parsed = PaymentProvider.parse_payload(query.invoice_payload)
    err = None
    if parsed is None:
        err = "Некорректный счёт."
    else:
        prov_name, pkg_id, uid = parsed
        prov = _providers().get(prov_name)
        pkg = get_package(pkg_id)
        if uid != query.from_user.id:
            err = "Счёт выписан другому пользователю."
        elif prov is None or pkg is None:
            err = "Этот пакет больше недоступен."
        elif query.currency != prov.currency or pkg.price(prov.currency) != query.total_amount:
            err = "Цена изменилась. Откройте /buy заново."
        elif not await user_allowed(query.from_user.id):
            err = "Доступ запрещен."
    if err:
        await query.answer(ok=False, error_message=err)
    else:
        await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message):
    pay = message.successful_payment
    uid = message.from_user.id
    parsed = PaymentProvider.parse_payload(pay.invoice_payload)
    pkg = get_package(parsed[1]) if parsed else None
    if parsed is None or pkg is None or parsed[2] != uid:
        # деньги уже списаны, а пакет определить нельзя — не теряем платёж молча
        logger.error(
            "UNMATCHED PAYMENT user=%s payload=%r charge=%s %s %s",
            uid, pay.invoice_payload, pay.telegram_payment_charge_id, pay.total_amount, pay.currency,
        )
        await message.answer(
            "⚠️ Платёж получен, но пакет определить не удалось. Напишите в /paysupport, "
            f"указав номер: <code>{html.escape(pay.telegram_payment_charge_id)}</code>"
        )
        return

    rec = await service.record_payment(
        uid, pkg, parsed[0], pay.telegram_payment_charge_id, pay.total_amount, pay.currency
    )
    if rec is None:  # повторная доставка апдейта
        return
    balance = await service.get_balance(uid)
    await message.answer(
        f"✅ Оплата получена: <b>{html.escape(pkg.title)}</b>.\n"
        f"Баланс: <b>{balance}</b> сообщений. Платёж № <code>{rec.id}</code>."
    )


@router.message(Command("balance"), F.chat.type == "private")
async def cmd_balance(message: Message):
    uid = message.from_user.id
    balance = await service.get_balance(uid)
    lines = [f"💰 Баланс: <b>{balance}</b> сообщений сверх бесплатного лимита."]
    pays = await service.recent_payments(uid, 3)
    if pays:
        lines.append("\nПоследние платежи:")
        for p in pays:
            lines.append(
                f"№ <code>{p.id}</code> · {html.escape(p.package_id)} · "
                f"{_fmt_price(p.currency, p.amount)} · {p.status}"
            )
    if _providers():
        lines.append("\nПополнить — /buy")
    await message.answer("\n".join(lines))


@router.message(Command("paysupport"), F.chat.type == "private")
async def cmd_paysupport(message: Message):
    await message.answer(
        "🛟 <b>Поддержка по платежам</b>\n\n"
        "Если оплата прошла, а баланс не пополнился, или нужен возврат — напишите администратору "
        "бота и укажите номер платежа из /balance. Возврат возможен, пока кредиты не потрачены."
    )


# ------------------------------- админ ----------------------------------- #
@router.message(Command("grant"), F.chat.type == "private")
async def cmd_grant(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].lstrip("-").isdigit() or not args[1].isdigit() or int(args[1]) <= 0:
        await message.answer("Формат: /grant <telegram_id> <кредитов>")
        return
    new_balance = await service.grant_credits(int(args[0]), int(args[1]))
    await message.answer(f"✅ Начислено {int(args[1])}. Баланс {args[0]}: {new_balance}")


@router.message(Command("refund"), F.chat.type == "private")
async def cmd_refund(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /refund <номер платежа>")
        return
    pay = await service.get_payment(int(arg))
    if pay is None:
        await message.answer("Платёж не найден.")
        return
    prov = _providers().get(pay.provider)
    if prov is None:
        await message.answer(f"Провайдер «{html.escape(pay.provider)}» выключен — возврат невозможен.")
        return

    status = await service.reverse_payment(pay.id)
    if status == "already_refunded":
        await message.answer("Платёж уже возвращён.")
        return
    if status == "credits_spent":
        await message.answer("⛔ Кредиты уже потрачены — возврат отклонён (баланс меньше купленного).")
        return
    if status != "ok":
        await message.answer("Платёж не найден.")
        return

    try:
        auto = await prov.refund(message.bot, pay.telegram_id, pay.charge_id)
    except Exception as e:
        logger.error("refund failed for payment %s: %s", pay.id, e)
        await service.restore_payment(pay.id)
        await message.answer("⚠️ Провайдер отклонил возврат; кредиты восстановлены. Подробности в логах.")
        return
    if auto:
        await message.answer(f"✅ Платёж № {pay.id} возвращён, кредиты сняты.")
    else:
        await message.answer(
            f"✅ Кредиты по платежу № {pay.id} сняты. Деньги верните вручную в кабинете "
            f"{html.escape(prov.label)} (charge: <code>{html.escape(pay.charge_id)}</code>)."
        )
