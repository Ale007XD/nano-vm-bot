"""
Ленивая загрузка и выгрузка эмбеддинг-модели (app/core/memory.py).
fastembed и ChromaDB замоканы: проверяется жизненный цикл, а не качество векторов.
"""
import asyncio
import importlib.util
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

mem = sys.modules["app.core.memory"]


class FakeModel:
    def embed(self, texts):
        for _ in texts:
            v = MagicMock()
            v.tolist.return_value = [0.5, 0.5]
            yield v


@pytest.fixture
def built(monkeypatch):
    """Счётчик созданий модели."""
    created = []

    def factory(model_name=None):
        time.sleep(0.02)  # чтобы гонки при загрузке были заметны
        m = FakeModel()
        created.append(m)
        return m

    monkeypatch.setattr(mem, "TextEmbedding", factory)
    return created


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "VECTOR_STORE_PATH", str(tmp_path / "vs"))
    monkeypatch.setattr(st, "EMBEDDING_IDLE_UNLOAD_SECONDS", 600, raising=False)
    return st


def new_memory():
    return mem.VectorMemory()


class TestLazyLoad:
    def test_not_loaded_until_first_use(self, built, cfg):
        vm = new_memory()
        assert vm.embedding_model is None and built == []
        assert vm._embed_text("a") == [0.5, 0.5]
        assert len(built) == 1

    def test_loaded_once_and_reused(self, built, cfg):
        vm = new_memory()
        for _ in range(5):
            vm._embed_text("x")
        assert len(built) == 1

    def test_concurrent_first_use_loads_once(self, built, cfg):
        vm = new_memory()
        threads = [threading.Thread(target=vm._embed_text, args=("t",)) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(built) == 1 and vm._model_busy == 0

    def test_busy_counter_released_on_error(self, built, cfg, monkeypatch):
        vm = new_memory()
        vm._embed_text("warm")

        def boom(texts):
            raise RuntimeError("onnx failed")

        monkeypatch.setattr(vm.embedding_model, "embed", boom)
        with pytest.raises(RuntimeError):
            vm._embed_text("x")
        assert vm._model_busy == 0

    def test_batch_embeddings(self, built, cfg):
        vm = new_memory()
        assert vm.embed_texts_sync(["a", "b", "c"]) == [[0.5, 0.5]] * 3

    def test_prepared_model_is_used_as_is(self, cfg):
        """Тесты/код могут подставить готовую модель — она не перезагружается."""
        vm = new_memory()
        vm.embedding_model = FakeModel()
        assert vm._embed_text("x") == [0.5, 0.5]


class TestUnload:
    def test_not_unloaded_before_idle(self, built, cfg):
        vm = new_memory()
        vm._embed_text("x")
        assert vm.unload_model_if_idle(600) is False
        assert vm.embedding_model is not None

    def test_unloaded_after_idle(self, built, cfg):
        vm = new_memory()
        vm._embed_text("x")
        later = time.monotonic() + 601
        assert vm.unload_model_if_idle(600, now=later) is True
        assert vm.embedding_model is None

    def test_not_unloaded_while_busy(self, built, cfg):
        vm = new_memory()
        vm._acquire_model()
        assert vm.unload_model_if_idle(0, now=time.monotonic() + 9999) is False
        vm._release_model()
        assert vm.unload_model_if_idle(0, now=time.monotonic() + 9999) is True

    def test_unload_when_nothing_loaded(self, cfg):
        assert new_memory().unload_model_if_idle(0) is False

    def test_reload_after_unload(self, built, cfg):
        vm = new_memory()
        vm._embed_text("x")
        vm.unload_model_if_idle(0, now=time.monotonic() + 1)
        assert vm._embed_text("y") == [0.5, 0.5]
        assert len(built) == 2

    @pytest.mark.asyncio
    async def test_background_loop_unloads(self, built, cfg):
        vm = new_memory()
        vm._embed_text("x")
        task = asyncio.create_task(vm._unload_loop(idle_seconds=0.05, interval=0.02))
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if vm.embedding_model is None:
                    break
            assert vm.embedding_model is None
        finally:
            task.cancel()

    @pytest.mark.asyncio
    async def test_loop_survives_unload_errors(self, built, cfg, monkeypatch):
        vm = new_memory()
        calls = []

        def flaky(idle, now=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return False

        monkeypatch.setattr(vm, "unload_model_if_idle", flaky)
        task = asyncio.create_task(vm._unload_loop(idle_seconds=1, interval=0.01))
        try:
            await asyncio.sleep(0.15)
            assert len(calls) >= 2
        finally:
            task.cancel()


class TestInitialize:
    @pytest.mark.asyncio
    async def test_lazy_mode_does_not_load_at_startup(self, built, cfg):
        vm = new_memory()
        try:
            await vm.initialize()
            assert built == [] and vm.embedding_model is None
            assert vm._unload_task is not None
        finally:
            if vm._unload_task:
                vm._unload_task.cancel()

    @pytest.mark.asyncio
    async def test_zero_means_eager_and_no_unload_task(self, built, cfg, monkeypatch):
        monkeypatch.setattr(cfg, "EMBEDDING_IDLE_UNLOAD_SECONDS", 0, raising=False)
        vm = new_memory()
        await vm.initialize()
        assert len(built) == 1 and vm.embedding_model is not None
        assert vm._unload_task is None and vm._model_busy == 0

    def test_non_int_setting_treated_as_disabled(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "EMBEDDING_IDLE_UNLOAD_SECONDS", MagicMock(), raising=False)
        assert mem._idle_unload_seconds() == 0
        monkeypatch.setattr(st, "EMBEDDING_IDLE_UNLOAD_SECONDS", True, raising=False)
        assert mem._idle_unload_seconds() == 0
        monkeypatch.setattr(st, "EMBEDDING_IDLE_UNLOAD_SECONDS", 90, raising=False)
        assert mem._idle_unload_seconds() == 90


class TestValidatedAnswerSharesModel:
    def test_no_new_model_per_call(self, built, cfg, monkeypatch):
        spec = importlib.util.spec_from_file_location("va_skill", "skills/custom/validated_answer.py")
        va = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(va)

        vm = new_memory()
        monkeypatch.setattr(mem, "vector_memory", vm)
        for _ in range(3):
            assert va._embed_texts(["a", "b"]) == [[0.5, 0.5], [0.5, 0.5]]
        assert len(built) == 1  # раньше: по новой копии модели на каждый вызов
