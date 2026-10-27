"""
Короткая история диалога клиента с витриной (только в памяти процесса).

Зачем: при эскалации оператору уходит резюме, а в карточку — последние реплики.
Не хранится в БД намеренно: постоянное хранение переписок требует политики
хранения и удаления (см. docs/support-bot-design.md) и появится в отдельном
сервисе поддержки. Здесь — ограниченный по размеру и времени буфер.

Интерфейс (add / recent / clear) нарочно узкий: его реализует и будущее
постоянное хранилище, так что витрина и обработчики оператора не меняются.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Sequence, Tuple

ROLE_CLIENT = "client"
ROLE_BOT = "bot"

KIND_QUESTION = "question"
KIND_ANSWER = "answer"
KIND_NOT_FOUND = "not_found"
KIND_ERROR = "error"

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Turn:
    role: str
    text: str
    kind: str = ""
    sources: Tuple[str, ...] = ()
    ts: float = 0.0


class DialogHistory:
    def __init__(
        self,
        max_turns: int = 12,
        max_clients: int = 2000,
        ttl: float = 24 * 3600,
        text_limit: int = 600,
        clock=time.time,
    ):
        self.max_turns = max_turns
        self.max_clients = max_clients
        self.ttl = ttl
        self.text_limit = text_limit
        self._clock = clock
        self._data: "OrderedDict[int, Deque[Turn]]" = OrderedDict()

    def add(
        self,
        client_id: int,
        role: str,
        text: str,
        kind: str = "",
        sources: Sequence[str] = (),
    ) -> None:
        clean = _TAG_RE.sub("", text or "").strip()[: self.text_limit]
        turn = Turn(role, clean, kind, tuple(s[:120] for s in sources[:5]), self._clock())
        q = self._data.get(client_id)
        if q is None:
            q = deque(maxlen=self.max_turns)
            self._data[client_id] = q
        q.append(turn)
        self._data.move_to_end(client_id)
        while len(self._data) > self.max_clients:
            self._data.popitem(last=False)

    def recent(self, client_id: int, n: Optional[int] = None) -> List[Turn]:
        q = self._data.get(client_id)
        if not q:
            return []
        cutoff = self._clock() - self.ttl
        fresh = [t for t in q if t.ts >= cutoff]
        if len(fresh) != len(q):
            if fresh:
                self._data[client_id] = deque(fresh, maxlen=self.max_turns)
            else:
                del self._data[client_id]
        return fresh[-n:] if n else fresh

    def last_client_text(self, client_id: int) -> str:
        for t in reversed(self.recent(client_id)):
            if t.role == ROLE_CLIENT:
                return t.text
        return ""

    def clear(self, client_id: int) -> None:
        self._data.pop(client_id, None)


history = DialogHistory()
