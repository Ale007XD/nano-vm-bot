"""Распознавание ссылок на YouTube (общее для обработчика сообщений и навыка youtube_transcript)."""
import re

# Поддерживаются: youtu.be/ID, youtube.com/watch?v=ID (в т.ч. watch?feature=share&v=ID),
# /live/ID, /shorts/ID, /embed/ID, /v/ID; хосты www., m., music., youtube-nocookie.com.
YOUTUBE_RE = re.compile(
    r"(?:youtu\.be/|(?:www\.|m\.|music\.)?youtube(?:-nocookie)?\.com/"
    r"(?:watch\?(?:[^\s#]*?&)?v=|live/|shorts/|embed/|v/))"
    r"([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def extract_video_id(text: str):
    """ID видео (11 символов) из текста со ссылкой или None."""
    m = YOUTUBE_RE.search(text or "")
    return m.group(1) if m else None
