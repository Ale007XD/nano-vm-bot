"""Служебные скрипты: прогон живой модели и проверка сервера. Тестируется сама логика, без сети и боевых данных."""
import asyncio
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ev():
    return load("intake_live_eval")


@pytest.fixture(scope="module")
def sc():
    return load("server_check")


async def run_all(ev, llm):
    space = ev.build_space()
    return [await ev.run_case(c, llm, space) for c in ev.CASES]


class TestIntakeEval:
    def test_ideal_model_passes_everything(self, ev):
        runs = asyncio.run(run_all(ev, ev.oracle(ev.CASES)))
        bad = [(r.case.text, r.shown, r.verdict.error) for r in runs if not r.verdict.ok]
        assert not bad, bad

    def test_broken_model_is_caught(self, ev):
        runs = asyncio.run(run_all(ev, ev.broken()))
        ok = sum(r.verdict.ok for r in runs)
        assert ok < len(runs) // 2
        inj = [r for r in runs if "казино" in r.case.text][0]
        assert not inj.verdict.ok and inj.verdict.extra == 1  # исполненная инъекция не проходит

    def test_judge_is_strict_about_extras_and_fields(self, ev):
        case = next(c for c in ev.CASES if c.text == "потратил 1200 на продукты")
        ctx_runs = asyncio.run(run_all(ev, ev.oracle(ev.CASES)))
        good = next(r for r in ctx_runs if r.case is case)
        assert good.verdict.ok
        wrong_amount = ev.Case("x", "x", [{"type": "expense", "amount": 999}])
        assert not ev.judge(wrong_amount, _proposals(ev, ev.CASES[11])).ok
        none_expected = ev.Case("x", "x", [])
        assert ev.judge(none_expected, _proposals(ev, case)).extra == 1

    def test_cases_are_wellformed(self, ev):
        texts = [c.text for c in ev.CASES]
        assert len(texts) == len(set(texts)) and len(texts) >= 25
        assert {c.group for c in ev.CASES} >= {"reminder", "shopping", "expense", "record", "mixed", "neutral", "injection"}
        for c in ev.CASES:
            assert bool(c.expect) == bool(c.ideal) or c.group in ("neutral", "injection")

    def test_report_has_metrics(self, ev):
        runs = asyncio.run(run_all(ev, ev.oracle(ev.CASES)))
        by = {}
        for r in runs:
            by.setdefault(r.case.text, []).append(r)
        md = ev.report(by, 1, "m", "t")
        assert "Успешных прогонов (строго)" in md and "100%" in md and "p50" in md and "## Фразы" in md


def _proposals(ev, case):
    from app.spaces import intake

    ctx = asyncio.run(intake.make_context(ev.build_space(), ev.MEMBERS, ev.NOW))
    import json

    return intake.parse_response(json.dumps({"items": case.ideal}, ensure_ascii=False), ctx).proposals


class TestServerCheck:
    def test_every_declared_feature_has_a_check_and_ids_unique(self, sc):
        feats = {c.feature for c in sc.REGISTRY}
        assert not [f for f in sc.FEATURES if f not in feats], "заявленные возможности без проверки"
        assert feats <= set(sc.FEATURES), feats - set(sc.FEATURES)
        ids = [c.id for c in sc.REGISTRY]
        assert len(ids) == len(set(ids))

    def test_status_mapping(self, sc):
        async def ok():
            return "fine"

        async def skip():
            raise sc.Skip("нет ключа")

        async def warn():
            raise sc.Warn("мало места")

        async def boom():
            raise RuntimeError("упало")

        async def assertion():
            raise AssertionError("не сошлось")

        async def slow():
            await asyncio.sleep(5)

        def mk(fn, needs=(), timeout=1):
            return sc.Check("1", "g", "t", "f", needs, timeout, fn)

        async def go():
            cs = [mk(ok), mk(skip), mk(warn), mk(boom), mk(assertion), mk(slow, timeout=0.05), mk(ok, needs=("net",))]
            for c in cs:
                await sc.run_check(c, {"net"})
            return [c.status for c in cs], cs

        got, cs = asyncio.run(go())
        assert got == [sc.PASS, sc.SKIP, sc.WARN, sc.FAIL, sc.FAIL, sc.FAIL, sc.SKIP]
        assert "упало" in cs[3].detail and "таймаут" in cs[5].detail and "флагом" in cs[6].detail

    def test_secrets_are_masked(self, sc):
        tok = "123456789:" + "A" * 35
        out = sc.mask(f"401 for https://api.telegram.org/bot{tok}/getMe and Bearer sk-abcdefghijklmnopqrstuv")
        assert tok not in out and "sk-abcdefghij" not in out and "api.telegram.org" in out
        assert sc.mask("sqlite3.OperationalError: no such table") == "sqlite3.OperationalError: no such table"

    def test_render_metrics(self, sc):
        def mk(i, status, feature):
            c = sc.Check(str(i), "g", f"проверка {i}", feature, (), 1, None)
            c.status, c.detail = status, "детали"
            return c

        feats = sc.FEATURES[:3]
        checks = [mk(1, sc.PASS, feats[0]), mk(2, sc.PASS, feats[0]), mk(3, sc.FAIL, feats[1]), mk(4, sc.WARN, feats[2]),
                  mk(5, sc.SKIP, feats[2])]
        md = sc.render(checks, "t", 1.0)
        assert "**67%** (2 из 3)" in md  # 2 PASS / (2 PASS + 1 FAIL)
        assert "✅ 2 · ❌ 1 · ⚠️ 1 · ⏭ 1" in md and "🔴" in md
        assert "❌ не работает" in md and "⚠️ с замечанием" in md and "нет автоматической проверки" in md
        green = sc.render([mk(1, sc.PASS, feats[0])], "t", 1.0)
        assert "🟢 всё работает" in green

    def test_unknown_group_exit_code(self, sc, tmp_path):
        import argparse

        a = argparse.Namespace(only="нет_такой_группы", offline=True, skip_llm=True, strict=False, out=str(tmp_path / "r.md"))
        assert asyncio.run(sc.main_async(a)) == 2


class TestMore:
    def test_judge_checks_time_with_tolerance(self, ev):
        from types import SimpleNamespace as NS

        p = NS(type="reminder", text="x", note="", run_at="2026-11-02T02:25:00", cron=None, assignee_id=None)
        strict = ev.Case("g", "t", [{"type": "reminder", "run_at": "2026-11-02T02:20:00"}])
        loose = ev.Case("g", "t", [{"type": "reminder", "run_at": "2026-11-02T02:20:00", "tol": 10}])
        assert not ev.judge(strict, [p]).ok and ev.judge(loose, [p]).ok
        far = NS(**{**p.__dict__, "run_at": "2026-11-02T04:00:00"})
        assert not ev.judge(loose, [far]).ok

    def test_failure_detail_is_masked(self, sc):
        tok = "123456789:" + "B" * 35

        async def boom():
            raise RuntimeError(f"GET https://api.telegram.org/bot{tok}/getMe -> 401")

        c = sc.Check("1", "g", "t", "f", (), 1, boom)
        asyncio.run(sc.run_check(c, set()))
        assert c.status == sc.FAIL and tok not in c.detail and "***" in c.detail

        async def asserting():
            raise AssertionError(f"ответ: {tok}")

        c2 = sc.Check("2", "g", "t", "f", (), 1, asserting)
        asyncio.run(sc.run_check(c2, set()))
        assert tok not in c2.detail

    @pytest.mark.parametrize("status,strict,code", [("PASS", False, 0), ("FAIL", False, 1), ("WARN", False, 0), ("WARN", True, 1)])
    def test_exit_codes(self, sc, tmp_path, monkeypatch, status, strict, code):
        import argparse

        async def fn():
            if status == "FAIL":
                raise AssertionError("x")
            if status == "WARN":
                raise sc.Warn("x")

        monkeypatch.setattr(sc, "REGISTRY", [sc.Check("1", "g", "title", sc.FEATURES[0], (), 1, fn)])
        a = argparse.Namespace(only="", offline=True, skip_llm=True, strict=strict, out=str(tmp_path / "r.md"))
        assert asyncio.run(sc.main_async(a)) == code
        assert (tmp_path / "r.md").read_text(encoding="utf-8").startswith("# Проверка сервера")


@pytest.mark.parametrize("name", ["server_check", "intake_live_eval"])
class TestEnsureEnv:
    def test_missing_deps_explained_not_33_failures(self, name, tmp_path, monkeypatch, capsys):
        mod = load(name)
        monkeypatch.setattr(mod, "ROOT", str(tmp_path))
        monkeypatch.setattr("importlib.util.find_spec", lambda m: None)
        with pytest.raises(SystemExit) as e:
            mod.ensure_env(f"{name}.py")
        err = capsys.readouterr().err
        assert e.value.code == 2 and "docker compose exec bot python" in err and "pydantic" in err

    def test_reexecs_into_project_venv(self, name, tmp_path, monkeypatch):
        mod = load(name)
        py = tmp_path / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("")
        monkeypatch.setattr(mod, "ROOT", str(tmp_path))
        monkeypatch.setattr("importlib.util.find_spec", lambda m: None)
        called = {}

        def fake_execv(p, a):  # настоящий execv не возвращается
            called.update(p=p, a=a)
            raise KeyboardInterrupt

        monkeypatch.setattr(mod.os, "execv", fake_execv)
        with pytest.raises(KeyboardInterrupt):
            mod.ensure_env(f"{name}.py")
        assert called["p"] == str(py) and called["a"][1].endswith(f"scripts/{name}.py")

    def test_nothing_to_do_when_deps_present(self, name):
        load(name).ensure_env(f"{name}.py")  # в тестовом окружении всё установлено


class TestInfraHandling:
    def test_classification_and_wait(self, ev):
        assert ev.is_infra(Exception("groq API error 429: Rate limit reached"))
        assert ev.is_infra(Exception("All providers failed. Last error: openrouter 429"))
        assert not ev.is_infra(ValueError("не JSON"))
        assert ev.retry_after(Exception("Please try again in 7.5s")) == 8.5
        assert ev.retry_after(Exception("try again in 400ms")) == 1.4
        assert ev.retry_after(Exception("try again in 600s")) == 30.0  # потолок
        assert ev.retry_after(Exception("429")) == 8.0

    def test_resilient_retries_then_gives_up(self, ev):
        waits, calls = [], {"n": 0}

        async def sleep(s):
            waits.append(s)

        async def flaky(system, user):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("429 try again in 2s")
            return "ok"

        assert asyncio.run(ev.resilient(flaky, 4, sleep)("s", "u")) == "ok" and waits == [3.0, 3.0]

        async def dead(system, user):
            raise Exception("429 rate limit")

        with pytest.raises(ev.InfraError):
            asyncio.run(ev.resilient(dead, 2, sleep)("s", "u"))

        async def bug(system, user):
            raise KeyError("x")

        n = len(waits)
        with pytest.raises(KeyError):
            asyncio.run(ev.resilient(bug, 3, sleep)("s", "u"))
        assert len(waits) == n  # не-инфра ошибка повторов не вызывает

    def test_infra_runs_are_excluded_from_accuracy(self, ev):
        async def dead(system, user):
            raise Exception("429 rate limit")

        space = ev.build_space()
        good = asyncio.run(ev.run_case(ev.CASES[0], ev.oracle(ev.CASES), space))
        gone = asyncio.run(ev.run_case(ev.CASES[1], dead, space, retries=0))
        assert good.verdict.ok and gone.verdict.infra and not gone.verdict.ok
        md = ev.report({ev.CASES[0].text: [good], ev.CASES[1].text: [gone]}, 1, "m", "t")
        assert "**1/1 (100%)**" in md and "исключено из точности" in md and "1/2" in md and "⏳" in md

    def test_exit_code_3_when_provider_mostly_silent(self, ev, tmp_path, monkeypatch):
        import argparse

        async def dead(system, user):
            raise Exception("429 rate limit")

        monkeypatch.setattr(ev, "live_llm", lambda model: dead)
        a = argparse.Namespace(only="reminder", selftest=False, selftest_broken=False, model="", parallel=1, retries=0,
                               repeat=1, min_pass=0.8, out=str(tmp_path / "r.md"))
        assert asyncio.run(ev.main_async(a)) == 3


class TestServerRetry:
    def test_retry_helper(self, sc, monkeypatch):
        async def nosleep(s):
            pass

        monkeypatch.setattr(sc.asyncio, "sleep", nosleep)
        n = {"i": 0}

        async def flaky():
            n["i"] += 1
            if n["i"] < 2:
                raise Exception("429 try again in 1s")
            return "ok"

        assert asyncio.run(sc._retry(flaky)) == "ok"

        async def dead():
            raise Exception("groq 429 rate limit")

        with pytest.raises(sc.Warn):  # лимит провайдера — замечание, не поломка
            asyncio.run(sc._retry(dead))

        async def bug():
            raise KeyError("x")

        with pytest.raises(KeyError):
            asyncio.run(sc._retry(bug))


def _handler():
    sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"  # планировщик читает её при импорте хендлеров
    from app.handlers import intake as h

    return h


class TestIntakeLlmWrapper:
    def _patch(self, monkeypatch, chat):
        from app.core import llm_client_v2

        monkeypatch.setattr(llm_client_v2.llm_client, "chat", chat)

    def test_json_validate_failed_means_nothing_to_record(self, monkeypatch):
        h = _handler()

        async def refuse(*a, **k):
            raise Exception('groq API error 400: {"code":"json_validate_failed","failed_generation":"I can\'t comply"}')

        self._patch(monkeypatch, refuse)
        assert asyncio.run(h._llm("s", "u")) == '{"items": []}'

    def test_other_errors_propagate_and_reasoning_is_low(self, monkeypatch):
        from types import SimpleNamespace

        h = _handler()
        seen = {}

        async def ok(messages, **k):
            seen.update(k)
            return SimpleNamespace(content='{"items": []}')

        self._patch(monkeypatch, ok)
        monkeypatch.setattr(h.settings, "SPACE_INTAKE_REASONING", "low", raising=False)
        assert asyncio.run(h._llm("s", "u")) == '{"items": []}' and seen["reasoning_effort"] == "low"

        async def boom(*a, **k):
            raise Exception("429 rate limit")

        self._patch(monkeypatch, boom)
        with pytest.raises(Exception, match="429"):
            asyncio.run(h._llm("s", "u"))
