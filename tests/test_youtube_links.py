"""Ссылки YouTube: все распространённые формы распознаются и обработчиком, и навыком."""
import importlib.util
import os

import pytest

from app.utils.youtube import YOUTUBE_RE, extract_video_id

ID = "8cs2wf6QwVk"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/live/{ID}?si=O_Yzkx3tgZHD-XEC",     # трансляция — случай из эксплуатации
    f"https://youtube.com/live/{ID}",
    f"https://www.youtube.com/watch?v={ID}",
    f"https://www.youtube.com/watch?feature=share&v={ID}&t=10",
    f"https://m.youtube.com/watch?v={ID}",
    f"https://music.youtube.com/watch?v={ID}&list=x",
    f"https://youtu.be/{ID}?t=5",
    f"https://www.youtube.com/shorts/{ID}",
    f"https://www.youtube.com/embed/{ID}",
    f"https://www.youtube-nocookie.com/embed/{ID}",
    f"смотри https://youtu.be/{ID} это важно",
])
def test_recognised(url):
    assert YOUTUBE_RE.search(url)
    assert extract_video_id(url) == ID


@pytest.mark.parametrize("text", [
    "https://example.com/watch?v=" + ID,
    "https://www.youtube.com/@channel",
    "https://www.youtube.com/watch?v=short",
    "https://youtu.be/",
    "привет",
    "",
])
def test_not_recognised(text):
    assert YOUTUBE_RE.search(text) is None
    assert extract_video_id(text) is None


def test_skill_uses_shared_extractor():
    spec = importlib.util.spec_from_file_location("_t_yt", os.path.join("skills", "custom", "youtube_transcript.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.extract_video_id(f"https://www.youtube.com/live/{ID}?si=x") == ID


def test_handler_uses_shared_regex():
    import sys

    sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"  # нужен при импорте обработчиков
    from app.handlers import messages

    assert messages.YOUTUBE_RE is YOUTUBE_RE
