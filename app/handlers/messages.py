"""
Message handler for chat conversations
"""
import os
import re

from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from app.core.database import async_session_maker, get_or_create_user, save_message
from app.core.config import settings
from app.agents.router import agent_router
from app.handlers.commands import get_user_agent_mode, get_allowed_users
from app.utils.helpers import sanitize_html
import base64
import httpx
import logging

# --- Runtime VM (agent_mode == "runtime") ---
from app.runtime import ExecutionVM, VMContext, StateContext, MultiProviderLLMAdapter, default_registry
from app.runtime.planner import Planner
from app.runtime.tool_registry import ToolRegistry
from app.core.llm_client_v2 import llm_client
from app.core.memory import vector_memory
from app.core.skills_loader import skill_loader

_vm_registry = default_registry()
_vm = ExecutionVM(_vm_registry)
_llm_adapter = MultiProviderLLMAdapter(llm_client)
_planner = Planner(_llm_adapter)
_tool_registry = ToolRegistry(skill_loader)

logger = logging.getLogger(__name__)

router = Router()

YOUTUBE_RE = re.compile(r'(youtu\.be/|youtube\.com/(watch\?v=|shorts/))[\w\-]+')

# Триггеры для включения self-check в vision
VISION_VERIFY_TRIGGERS = [
    'найди', 'поиск', 'где купить', 'что это', 'определи',
    'распознай', 'прочитай', 'что написано', 'какой текст',
    'это место', 'что за', 'идентифицируй', 'сколько стоит',
    'найти', 'покажи похожие', 'что на фото', 'опознай'
]


@router.message(F.text)
async def handle_message(message: Message, state: FSMContext):
    """Handle incoming text messages"""
    user = message.from_user
    user_message = message.text

    logger.info(f"Message handler triggered: '{user_message}' from user {user.id}")

    if user.id not in get_allowed_users():
        logger.warning(f"Access denied for user {user.id}")
        await message.answer(
            "⛔ <b>Доступ запрещен</b>\n\n"
            "Этот бот является приватным. У вас нет прав на использование."
        )
        return

    if user_message.startswith('/') or user_message in [
        "💬 Чат", "🤖 Режим", "🛠 Навыки", "🧠 Память", "❓ Помощь", "⚙️ Настройки"
    ]:
        logger.info(f"Skipping menu button: {user_message}")
        return

    try:
        async with async_session_maker() as session:
            db_user = await get_or_create_user(
                session,
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code
            )

            agent_mode = await get_user_agent_mode(user.id)

            from app.core.database import get_user_messages
            history = await get_user_messages(session, db_user.id, limit=20)
            conversation_history = [
                {"role": msg.role, "content": msg.content}
                for msg in reversed(history)
            ]

            await message.bot.send_chat_action(message.chat.id, "typing")

            # --- YouTube автодетект ---
            user_message_for_agent = user_message
            if YOUTUBE_RE.search(user_message):
                from app.core.skills_loader import skill_loader
                yt_skill = skill_loader.get_skill("youtube_transcript")
                if yt_skill:
                    logger.info(f"YouTube link detected, fetching transcript for user {user.id}")
                    transcript = await yt_skill({
                        "user_id": user.id,
                        "message_text": user_message
                    })
                    if str(transcript).startswith("❌"):
                        await message.answer(f"Не удалось получить транскрипт видео.\n{transcript}")
                        return
                    user_message_for_agent = (
                        f"[ссылка на видео]\n\n[ТРАНСКРИПТ ВИДЕО]:\n{transcript}"
                    )

            # --- Knowledge Base: автодетект forward с URL ---
            elif message.forward_origin is not None:
                _url_m = re.search(r'https?://[^\s\]\)>"\']+', user_message)
                if _url_m:
                    from app.core.skills_loader import skill_loader
                    kb_skill = skill_loader.get_skill("knowledge_base")
                    if kb_skill:
                        logger.info(f"Forward+URL → knowledge_base for user {user.id}")
                        await message.bot.send_chat_action(message.chat.id, "typing")
                        kb_result = await kb_skill({
                            "user_id": user.id,
                            "message_text": user_message,
                            "args": {"url": _url_m.group(0).rstrip(".,;)")}
                        })
                        await message.answer(sanitize_html(kb_result))
                        return

            # --- Runtime branch (agent_mode == "runtime") ---
            if agent_mode == "runtime":
                from sqlalchemy import select
                from app.core.database import UserState

                # Загрузить или создать UserState для персистентности StateContext
                _us_result = await session.execute(
                    select(UserState).where(UserState.user_id == db_user.id)
                )
                db_user_state = _us_result.scalar_one_or_none()
                if db_user_state is None:
                    db_user_state = UserState(user_id=db_user.id, current_agent="runtime")
                    session.add(db_user_state)
                    await session.flush()

                runtime_state = StateContext.from_db(db_user_state)
                # StateContext.user_id по контракту — Telegram ID (ключ в VectorMemory),
                # тогда как UserState.user_id — internal DB id
                runtime_state = runtime_state.model_copy(update={"user_id": user.id})
                vm_ctx = VMContext(
                    state=runtime_state,
                    llm=_llm_adapter,
                    memory=vector_memory,
                    tools=_tool_registry,
                )

                # --- RAG: Inject context into Planner ---
                try:
                    context_data = await vector_memory.get_relevant_context(
                        query=user_message_for_agent,
                        user_id=user.id,
                        n_memories=10,
                        n_conversations=1
                    )
                    memories = context_data.get('memories', [])
                    if memories:
                        mem_texts = [f"- {m['content']} (сохранено: {m['metadata'].get('created_at', '')[:10]})" for m in memories]
                        rag_block = "\n\n[СИСТЕМНЫЙ КОНТЕКСТ: ИЗВЕСТНЫЕ ФАКТЫ / ВОСПОМИНАНИЯ]\n" + "\n".join(mem_texts)
                        user_message_for_agent += rag_block
                        logger.info(f"Injected {len(memories)} memories into Planner context for user {db_user.id}")
                except Exception as e:
                    logger.warning(f"RAG extraction failed: {e}")

                program = await _planner.generate(
                    user_input=user_message_for_agent,
                    history=conversation_history,
                )
                run_result = await _vm.run(program, vm_ctx)

                # Персистировать обновлённый StateContext → UserState.context
                db_user_state.context = run_result.state.to_db_context()
                await session.flush()

                response = "\n".join(
                    entry.text for entry in run_result.outbox
                ) or "⚠️ Runtime: пустой outbox."
            # --- Legacy branch (fastbot / planbot / skillbot) ---
            else:
                response = await agent_router.route_message(
                    user_id=user.id,
                    message=user_message_for_agent,
                    agent_mode=agent_mode,
                    conversation_history=conversation_history,
                )

            await save_message(
                session,
                user_id=db_user.id,
                role="user",
                content=user_message,
                agent_mode=agent_mode
            )
            await save_message(
                session,
                user_id=db_user.id,
                role="assistant",
                content=response,
                agent_mode=agent_mode
            )

        response = sanitize_html(response)
        if len(response) > 4096:
            # 4000 + заголовок продолжения гарантированно укладывается в лимит 4096
            chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await message.answer(chunk)
                else:
                    await message.answer(f"<i>(continued {i+1}/{len(chunks)})</i>\n\n{chunk}")
        else:
            await message.answer(response)

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await message.answer(
            "⚠️ <b>Извините, произошла ошибка при обработке вашего сообщения.</b>\n\n"
            "Пожалуйста, попробуйте снова или используйте /help для помощи."
        )


@router.message(F.photo)
async def handle_photo(message: Message):
    """Handle photo messages — analyze with vision model + optional self-check"""
    user = message.from_user

    if user.id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен")
        return

    processing_msg = await message.answer("🔍 Анализирую изображение...")

    try:
        if not settings.GROQ_API_KEY:
            await processing_msg.edit_text(
                "❌ <b>Анализ изображений недоступен</b>\n\n"
                "Vision-модель работает через Groq: задайте <code>GROQ_API_KEY</code> в .env "
                "и перезапустите бота."
            )
            return

        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)

        photo_url = f"https://api.telegram.org/file/bot{message.bot.token}/{file.file_path}"

        async with httpx.AsyncClient() as dl_client:
            dl_resp = await dl_client.get(photo_url, timeout=30.0)
            dl_resp.raise_for_status()
            image_data = dl_resp.content

        image_b64 = base64.b64encode(image_data).decode("utf-8")
        user_prompt = message.caption or "Подробно опиши что изображено на фото. Отвечай на русском языке."

        headers = {
            "Authorization": f"Bearer {settings.GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

        def make_vision_payload(prompt: str, temperature: float = 0.7) -> dict:
            return {
                "model": settings.VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ]
                    }
                ],
                "max_tokens": 1024,
                "temperature": temperature
            }

        # Первый проход — основной ответ
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=make_vision_payload(user_prompt),
                timeout=30.0
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"]

        # Self-check — только для поисковых/идентификационных запросов
        needs_check = any(w in user_prompt.lower() for w in VISION_VERIFY_TRIGGERS)

        if needs_check:
            logger.info(f"Vision self-check triggered for user {user.id}")
            await processing_msg.edit_text("🔍 Проверяю результат...")

            verify_prompt = f"""Запрос пользователя: {user_prompt}

Мой первый ответ:
{result}

Проверь ответ по изображению:
1. Всё ли точно? Нет ли галлюцинаций или домыслов?
2. Если запрос про поиск/идентификацию — достаточно ли конкретен ответ?
3. Если есть ошибки — исправь и дай финальный ответ.
4. Если ответ верный — верни его без изменений.

Верни только финальный ответ без объяснений проверки."""

            async with httpx.AsyncClient() as client:
                check_resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=make_vision_payload(verify_prompt, temperature=0.3),
                    timeout=30.0
                )
                check_resp.raise_for_status()
                final_result = check_resp.json()["choices"][0]["message"]["content"]

            await processing_msg.edit_text(
                f"🖼 <b>Анализ изображения:</b>\n\n{final_result}"
            )
        else:
            await processing_msg.edit_text(
                f"🖼 <b>Анализ изображения:</b>\n\n{result}"
            )

    except Exception as e:
        logger.error(f"Error analyzing photo: {e}")
        await processing_msg.edit_text(
            f"❌ <b>Не удалось проанализировать изображение</b>\n\n<code>{e}</code>"
        )


@router.message(F.document)
async def handle_document(message: Message):
    """Handle document messages — import Telegram JSON chat export"""
    user = message.from_user

    if user.id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен")
        return

    doc = message.document

    if not doc.file_name or not doc.file_name.endswith(".json"):
        await message.answer(
            "📄 <b>Документ получен</b>\n\n"
            "Поддерживается импорт JSON-экспорта Telegram.\n"
            "Экспортируйте чат: <b>Telegram Desktop → чат → ⋮ → Экспорт истории → JSON</b>\n"
            "и отправьте файл <code>result.json</code>"
        )
        return

    if doc.file_size and doc.file_size > 50 * 1024 * 1024:
        await message.answer("❌ Файл слишком большой. Максимум 50 МБ.")
        return

    processing_msg = await message.answer("⏳ Загружаю и обрабатываю файл...")
    temp_path = f"/tmp/import_{user.id}_{doc.file_id}.json"

    try:
        file = await message.bot.get_file(doc.file_id)
        await message.bot.download_file(file.file_path, temp_path)
        await processing_msg.edit_text("🔍 Анализирую историю чата...")

        from app.core.skills_loader import skill_loader
        skill = skill_loader.get_skill("import_chat")

        if not skill:
            await processing_msg.edit_text(
                "❌ Скилл import_chat не загружен.\n"
                "Убедитесь что файл <code>skills/custom/import_chat.py</code> существует."
            )
            return

        context = {
            "user_id": user.id,
            "file_path": temp_path,
            "args": {"file_path": temp_path}
        }

        result = await skill(context)
        await processing_msg.edit_text(result)

    except Exception as e:
        logger.error(f"Error processing document from user {user.id}: {e}")
        await processing_msg.edit_text(
            f"❌ <b>Ошибка обработки файла:</b>\n<code>{e}</code>"
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    """Handle voice messages with transcription"""
    user = message.from_user
    file_path = None

    logger.info(f"Voice message received from user {user.id}")

    if user.id not in get_allowed_users():
        logger.warning(f"Access denied for user {user.id}")
        await message.answer("⛔ Доступ запрещен")
        return

    chat_type = message.chat.type
    is_group = chat_type in ['group', 'supergroup']

    if is_group and not settings.ENABLE_VOICE_IN_GROUPS:
        logger.info("Voice processing disabled for groups")
        return

    temp_dir = settings.TEMP_DIR
    os.makedirs(temp_dir, exist_ok=True)

    try:
        voice = message.voice

        if voice.duration > settings.MAX_VOICE_DURATION:
            await message.answer(
                f"⚠️ <b>Слишком длинное голосовое сообщение</b>\n\n"
                f"Максимальная длительность: {settings.MAX_VOICE_DURATION} секунд\n"
                f"Ваше сообщение: {voice.duration} секунд\n\n"
                f"Пожалуйста, разбейте на части или отправьте текстом."
            )
            return

        await message.bot.send_chat_action(message.chat.id, "typing")

        voice_file = await message.bot.get_file(voice.file_id)
        file_path = os.path.join(temp_dir, f"voice_{user.id}_{voice.file_id}.ogg")

        logger.info(f"Downloading voice file to {file_path}")
        await message.bot.download_file(voice_file.file_path, file_path)

        processing_msg = await message.answer(
            "🎤 <i>Распознаю голосовое сообщение...</i>"
        )

        from app.core.llm_client_v2 import llm_client
        transcribed_text = await llm_client.transcribe_audio(
            audio_file_path=file_path,
            language=None
        )

        await processing_msg.delete()

        if not transcribed_text or transcribed_text.strip() == "":
            await message.answer(
                "🎤 <b>Не удалось распознать речь</b>\n\n"
                "Попробуйте:\n"
                "• Говорить четче\n"
                "• Уменьшить фоновый шум\n"
                "• Отправить текстовое сообщение"
            )
            return

        if is_group:
            user_name = user.first_name or "Пользователь"
            if user.username:
                user_name = f"@{user.username}"
            await message.reply(
                f"🎤 <b>Распознано ({user_name}):</b>\n"
                f"<i>{transcribed_text}</i>\n\n"
                f"⏳ Обрабатываю...",
                quote=True
            )
        else:
            await message.answer(
                f"🎤 <b>Распознано:</b>\n<i>{transcribed_text}</i>"
            )

        logger.info(f"Transcribed voice for user {user.id}: {transcribed_text[:100]}...")

        await message.bot.send_chat_action(message.chat.id, "typing")

        async with async_session_maker() as session:
            db_user = await get_or_create_user(
                session,
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code
            )

            agent_mode = await get_user_agent_mode(user.id)

            from app.core.database import get_user_messages
            history = await get_user_messages(session, db_user.id, limit=20)
            conversation_history = [
                {"role": msg.role, "content": msg.content}
                for msg in reversed(history)
            ]

            if agent_mode == "runtime":
                from sqlalchemy import select
                from app.core.database import UserState

                _us_result = await session.execute(
                    select(UserState).where(UserState.user_id == db_user.id)
                )
                db_user_state = _us_result.scalar_one_or_none()
                if db_user_state is None:
                    db_user_state = UserState(user_id=db_user.id, current_agent="runtime")
                    session.add(db_user_state)
                    await session.flush()

                runtime_state = StateContext.from_db(db_user_state)
                # StateContext.user_id по контракту — Telegram ID (ключ в VectorMemory),
                # тогда как UserState.user_id — internal DB id
                runtime_state = runtime_state.model_copy(update={"user_id": user.id})
                vm_ctx = VMContext(
                    state=runtime_state,
                    llm=_llm_adapter,
                    memory=vector_memory,
                    tools=_tool_registry,
                )

                try:
                    context_data = await vector_memory.get_relevant_context(
                        query=transcribed_text,
                        user_id=user.id,
                        n_memories=10,
                        n_conversations=1
                    )
                    memories = context_data.get('memories', [])
                    if memories:
                        mem_texts = [f"- {m['content']} (сохранено: {m['metadata'].get('created_at', '')[:10]})" for m in memories]
                        rag_block = "\n\n[СИСТЕМНЫЙ КОНТЕКСТ: ИЗВЕСТНЫЕ ФАКТЫ / ВОСПОМИНАНИЯ]\n" + "\n".join(mem_texts)
                        transcribed_text_for_agent = transcribed_text + rag_block
                    else:
                        transcribed_text_for_agent = transcribed_text
                except Exception as e:
                    logger.warning(f"RAG extraction failed: {e}")
                    transcribed_text_for_agent = transcribed_text

                program = await _planner.generate(
                    user_input=transcribed_text_for_agent,
                    history=conversation_history,
                )
                run_result = await _vm.run(program, vm_ctx)

                db_user_state.context = run_result.state.to_db_context()
                await session.flush()

                response = "\n".join(
                    entry.text for entry in run_result.outbox
                ) or "⚠️ Runtime: пустой outbox."
            else:
                response = await agent_router.route_message(
                    user_id=user.id,
                    message=transcribed_text,
                    agent_mode=agent_mode,
                    conversation_history=conversation_history
                )

            await save_message(
                session,
                user_id=db_user.id,
                role="user",
                content=f"[Голосовое] {transcribed_text}",
                agent_mode=agent_mode
            )
            await save_message(
                session,
                user_id=db_user.id,
                role="assistant",
                content=response,
                agent_mode=agent_mode
            )

        response = sanitize_html(response)
        if len(response) > 4096:
            # 4000 + заголовок продолжения гарантированно укладывается в лимит 4096
            chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await message.answer(chunk)
                else:
                    await message.answer(f"<i>(продолжение {i+1}/{len(chunks)})</i>\n\n{chunk}")
        else:
            await message.answer(response)

    except Exception as e:
        logger.error(f"Error handling voice message: {e}")
        await message.answer(
            "⚠️ <b>Ошибка обработки голосового сообщения.</b>\n\n"
            "Пожалуйста, попробуйте снова."
        )
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
