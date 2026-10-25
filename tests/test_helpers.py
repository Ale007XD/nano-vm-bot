"""
Регрессионные тесты багфиксов Фазы 2:
    sanitize_html (BUG-12) — экранирование и балансировка Telegram HTML
    crypto (BUG-14)        — источник ENCRYPTION_KEY
    web-search триггеры (BUG-19) — границы слов
    typing / текущее время — UX-фиксы (индикатор «печатает…», инъекция даты)
"""
import asyncio
import sys
import types

import pytest


class TestSanitizeHtml:
    def test_plain_text_untouched(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("просто текст") == "просто текст"

    def test_allowed_tags_preserved(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("<b>жирный</b>") == "<b>жирный</b>"

    def test_unclosed_tag_gets_closed(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("<b>текст") == "<b>текст</b>"

    def test_dangling_closing_tag_escaped(self):
        from app.utils.helpers import sanitize_html
        out = sanitize_html("текст</i> продолжение")
        assert "</i>" not in out
        assert "&lt;/i>" in out

    def test_unknown_tag_escaped(self):
        from app.utils.helpers import sanitize_html
        out = sanitize_html("<div>x</div>")
        assert "<div>" not in out
        assert "&lt;div>" in out

    def test_tag_attributes_stripped(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("<code class='python'>x</code>") == "<code>x</code>"

    def test_a_tag_keeps_href_only(self):
        from app.utils.helpers import sanitize_html
        out = sanitize_html('<a href="https://t.ru" onclick="x">ссылка</a>')
        assert out == '<a href="https://t.ru">ссылка</a>'

    def test_a_without_href_escaped(self):
        from app.utils.helpers import sanitize_html
        out = sanitize_html("<a>ссылка</a>")
        assert "<a>" not in out

    def test_bare_lt_escaped(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("a < b") == "a &lt; b"

    def test_crossed_tags_rebalanced(self):
        from app.utils.helpers import sanitize_html
        out = sanitize_html("<b><i>x</b></i>")
        assert out.count("<b>") == 1 and out.count("</b>") == 1
        assert out.count("<i>") == 1 and out.count("</i>") == 1


class TestMarkdownToHtml:
    """LLM шлёт Markdown, бот отправляет parse_mode=HTML — конвертация."""

    def test_bold(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("**26 октября**") == "<b>26 октября</b>"

    def test_italic_underscore(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("__важно__") == "<i>важно</i>"

    def test_inline_code(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("команда `pip install` тут") == "команда <code>pip install</code> тут"

    def test_header(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("## Итог\nтекст") == "<b>Итог</b>\nтекст"

    def test_code_block_becomes_pre_and_escaped(self):
        from app.utils.helpers import sanitize_html
        out = sanitize_html("```python\nif a < b:\n    pass\n```")
        assert out.startswith("<pre>")
        assert "if a &lt; b:" in out

    def test_markdown_inside_code_block_not_converted(self):
        from app.utils.helpers import sanitize_html
        out = sanitize_html("```\n**not bold**\n```")
        assert "<b>" not in out
        assert "**not bold**" in out

    def test_plain_text_untouched_by_md(self):
        from app.utils.helpers import sanitize_html
        assert sanitize_html("просто текст 2**10") == "просто текст 2**10"


class TestCryptoKeySource:
    def test_key_from_settings_when_env_missing(self, monkeypatch):
        pytest.importorskip("cryptography")
        from cryptography.fernet import Fernet

        valid_key = Fernet.generate_key().decode()
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

        fake_config = types.SimpleNamespace(
            settings=types.SimpleNamespace(ENCRYPTION_KEY=valid_key)
        )
        monkeypatch.setitem(sys.modules, "app.core.config", fake_config)

        from app.core import crypto
        f = crypto._get_fernet()
        assert f.encrypt(b"secret")

    def test_missing_key_raises(self, monkeypatch):
        pytest.importorskip("cryptography")
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

        fake_config = types.SimpleNamespace(
            settings=types.SimpleNamespace(ENCRYPTION_KEY="")
        )
        monkeypatch.setitem(sys.modules, "app.core.config", fake_config)

        from app.core import crypto
        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            crypto._get_fernet()


class TestWebSearchTrigger:
    """Триггеры веб-поиска должны матчиться по границам слов (BUG-19)."""

    def test_substring_word_does_not_trigger(self):
        from app.agents.fastbot import FastBotAgent
        agent = FastBotAgent()
        assert agent._should_search_web("никто не знает ответа") is False
        assert agent._should_search_web("прежде чем ответить") is False

    def test_real_trigger_words_still_work(self):
        from app.agents.fastbot import FastBotAgent
        agent = FastBotAgent()
        assert agent._should_search_web("кто такой Пушкин?") is True
        assert agent._should_search_web("найди рецепт борща") is True


class TestTypingIndicator:
    @pytest.mark.asyncio
    async def test_start_typing_repeats_until_stopped(self):
        from unittest.mock import AsyncMock

        from app.utils.helpers import start_typing, stop_typing

        bot = AsyncMock()
        task = start_typing(bot, chat_id=1, interval=0.05)
        await asyncio.sleep(0.18)
        stop_typing(task)
        await asyncio.sleep(0)

        assert bot.send_chat_action.await_count >= 2
        assert bot.send_chat_action.await_args.args[1] == "typing"

    @pytest.mark.asyncio
    async def test_stop_typing_is_safe_for_none_and_finished(self):
        from app.utils.helpers import stop_typing

        stop_typing(None)
        stop_typing(asyncio.Future())


class TestCurrentDatetimePrompt:
    def test_contains_weekday_time_and_tz(self):
        import re

        from app.utils.helpers import current_datetime_prompt

        prompt = current_datetime_prompt("Europe/Moscow")
        assert "Europe/Moscow" in prompt
        assert any(
            wd in prompt
            for wd in ("понедельник", "вторник", "среда", "четверг",
                       "пятница", "суббота", "воскресенье")
        )
        assert re.search(r"\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}", prompt)
