"""Копирование вывода скриптов в буфер обмена — одним вызовом, без внешних зависимостей.

Порядок попыток:
1. OSC 52 — escape-последовательность в ваш терминал: работает и по SSH, и из `docker exec -t`, буфер
   получает ЛОКАЛЬНЫЙ компьютер (Windows Terminal, iTerm2, kitty, WezTerm, Alacritty, tmux с set-clipboard on…).
   Пишется в /dev/tty, поэтому не попадает в перенаправленный stdout.
2. Утилиты рядом: wl-copy, xclip, xsel, pbcopy, clip.exe (WSL).

Без tty (`docker exec` без -t, cron) копировать некуда: скрипт скажет об этом и подскажет `-t`.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
from typing import Callable, List, Optional

OSC52_LIMIT = 100_000  # у части терминалов потолок около 100 КБ — длиннее не отправляем, чтобы не обрезало молча
TOOLS: List[List[str]] = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"], ["pbcopy"], ["clip.exe"]]


def osc52_sequence(text: str, env: Optional[dict] = None) -> str:
    env = os.environ if env is None else env
    seq = f"\033]52;c;{base64.b64encode(text.encode('utf-8')).decode('ascii')}\a"
    if env.get("TMUX"):  # tmux пропускает последовательность только в обёртке passthrough
        return "\033Ptmux;" + seq.replace("\033", "\033\033") + "\033\\"
    if env.get("TERM", "").startswith("screen"):
        return "\033P" + seq + "\033\\"
    return seq


def copy(text: str, *, tty_path: str = "/dev/tty", which: Callable = shutil.which,
         run: Callable = subprocess.run, env: Optional[dict] = None) -> str:
    """Скопировать text. Возвращает название способа ("osc52", "wl-copy", …) или "" — не удалось."""
    if not text:
        return ""
    if len(text.encode("utf-8")) <= OSC52_LIMIT:
        try:
            with open(tty_path, "w") as tty:
                tty.write(osc52_sequence(text, env))
                tty.flush()
            return "osc52"
        except OSError:
            pass
    for cmd in TOOLS:
        if which(cmd[0]):
            try:
                if run(cmd, input=text.encode("utf-8"), timeout=5, check=True).returncode == 0:
                    return cmd[0]
            except (OSError, subprocess.SubprocessError):
                continue
    return ""


def announce(text: str, enabled: bool = True, **kw) -> None:
    """Скопировать и сказать об этом одной строкой (в stderr, чтобы не портить вывод)."""
    import sys

    if not enabled:
        return
    how = copy(text, **kw)
    if how == "osc52":
        print("📋 Отчёт отправлен в буфер обмена терминала (OSC 52).", file=sys.stderr)
    elif how:
        print(f"📋 Отчёт скопирован в буфер ({how}).", file=sys.stderr)
    else:
        print("📋 Скопировать некуда: нет терминала. В Docker запускайте `docker exec -t …`; "
              "отчёт сохранён в файл (--no-copy отключает это сообщение).", file=sys.stderr)
