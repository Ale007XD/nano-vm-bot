"""
Скилл: получение транскрипта YouTube-видео
Использование: отправь боту ссылку на YouTube-видео
"""
import asyncio
import glob
import os
import re
import tempfile

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled


def extract_video_id(url: str) -> str | None:
    patterns = [
        r'(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


async def run(context: dict) -> str:
    text = context.get("message_text", "")
    video_id = extract_video_id(text)

    if not video_id:
        return "Не найден ID видео. Отправь ссылку вида https://youtu.be/xxx или https://youtube.com/watch?v=xxx"

    # Попытка 1: youtube-transcript-api
    try:
        api = YouTubeTranscriptApi()
        try:
            snippets = api.fetch(video_id, languages=['ru'])
        except Exception:
            snippets = api.fetch(video_id, languages=['en'])

        clean = ' '.join(s.text.strip() for s in snippets)
        if len(clean) > 3000:
            clean = clean[:3000] + '...\n[транскрипт обрезан]'
        return f"📄 Транскрипт [{video_id}]:\n\n{clean}"

    except (TranscriptsDisabled, NoTranscriptFound) as e:
        return f"❌ {e}"
    except Exception:
        pass  # IP banned — fallback to yt-dlp

    # Попытка 2: yt-dlp
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = await asyncio.create_subprocess_exec(
                'yt-dlp', '--write-auto-sub', '--skip-download',
                '--sub-lang', 'ru,en', '--convert-subs', 'srt',
                '-o', os.path.join(tmpdir, 'sub'),
                f'https://www.youtube.com/watch?v={video_id}',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            files = glob.glob(os.path.join(tmpdir, '*.srt'))
            if not files:
                return "❌ Транскрипт недоступен (субтитры не найдены)."
            with open(files[0], encoding='utf-8') as f:
                raw = f.read()
            # Убрать SRT-разметку
            clean = re.sub(r'\d+\n\d{2}:\d{2}.*?-->\s*\d{2}:\d{2}.*?\n', '', raw)
            clean = re.sub(r'<[^>]+>', '', clean).strip()
            clean = re.sub(r'\n{2,}', ' ', clean)
            if len(clean) > 3000:
                clean = clean[:3000] + '...\n[транскрипт обрезан]'
            return f"📄 Транскрипт [{video_id}]:\n\n{clean}"
    except Exception as e:
        return f"❌ Ошибка: \n{e}"
