"""scripts/migrate_env.py: перенос значений в раскладку .env.example."""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "migrate_env.py")


@pytest.fixture(scope="module")
def me():
    spec = importlib.util.spec_from_file_location("_migrate_env", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TEMPLATE = """\
# header
BOT_TOKEN=your_telegram_bot_token_here
GROQ_API_KEY=your_groq_api_key_here
FASTBOT_MODEL=openai/gpt-oss-20b
KB_STALE_DAYS=30
MCP_SERVERS=
# MCP_SERVERS=[{"name":"fetch"}]
APP_VERSION=1.10.1
ENCRYPTION_KEY=your_fernet_key_here
# FASTBOT_SYSTEM_PROMPT="..."
# SHOWCASE_OPEN_ACCESS=true
"""

PROMPT = '"Ты — Bot.\\n\\nПРАВИЛА\\n1. Ответ.\\n2. «Кавычки» и \'апострофы\'"'


def _run(me, tmp_path, old_text, template=TEMPLATE, monkeypatch=None, argv_extra=()):
    old, tpl, out = tmp_path / ".env", tmp_path / ".env.example", tmp_path / ".env.new"
    old.write_text(old_text, encoding="utf-8")
    tpl.write_text(template, encoding="utf-8")
    import sys
    argv = ["migrate_env", "--old", str(old), "--template", str(tpl), "--out", str(out), *argv_extra]
    monkeypatch.setattr(sys, "argv", argv)
    me.main()
    return out.read_text(encoding="utf-8"), old


def test_values_prompts_and_models_kept_verbatim(me, tmp_path, monkeypatch, capsys):
    old_text = (
        "BOT_TOKEN=123:abc\nGROQ_API_KEY=gsk_x\nFASTBOT_MODEL=my/model\n"
        f"FASTBOT_SYSTEM_PROMPT={PROMPT}\nSHOWCASE_OPEN_ACCESS=true\n"
    )
    out, _ = _run(me, tmp_path, old_text, monkeypatch=monkeypatch)
    lines = out.splitlines()
    assert "BOT_TOKEN=123:abc" in lines
    assert "FASTBOT_MODEL=my/model" in lines
    assert f"FASTBOT_SYSTEM_PROMPT={PROMPT}" in lines      # дословно, кавычки и \n нетронуты
    assert "SHOWCASE_OPEN_ACCESS=true" in lines             # из закомментированного примера
    assert "# FASTBOT_SYSTEM_PROMPT=" not in out            # пример заменён, а не продублирован


def test_new_keys_added_and_placeholders_commented(me, tmp_path, monkeypatch, capsys):
    out, _ = _run(me, tmp_path, "BOT_TOKEN=1:a\n", monkeypatch=monkeypatch)
    lines = out.splitlines()
    assert "KB_STALE_DAYS=30" in lines
    assert "# GROQ_API_KEY=your_groq_api_key_here" in lines     # плейсхолдер не становится значением
    assert "# ENCRYPTION_KEY=your_fernet_key_here" in lines
    assert 'MCP_SERVERS=' in lines and '# MCP_SERVERS=[{"name":"fetch"}]' in lines


def test_duplicate_last_wins_and_is_reported(me, tmp_path, monkeypatch, capsys):
    out, _ = _run(me, tmp_path, "GROQ_API_KEY=first\nBOT_TOKEN=1:a\nGROQ_API_KEY=second\n", monkeypatch=monkeypatch)
    assert out.count("GROQ_API_KEY=") == 1 and "GROQ_API_KEY=second" in out
    rep = capsys.readouterr().out
    assert "ДУБЛЬ GROQ_API_KEY" in rep and "РАЗНЫЕ" in rep
    assert "first" not in rep and "second" not in rep       # значения в отчёт не попадают


def test_app_version_taken_from_template(me, tmp_path, monkeypatch, capsys):
    out, _ = _run(me, tmp_path, "APP_VERSION=1.9.0\n", monkeypatch=monkeypatch)
    assert "APP_VERSION=1.10.1" in out.splitlines() and "1.9.0" not in out


def test_unknown_keys_appended(me, tmp_path, monkeypatch, capsys):
    out, _ = _run(me, tmp_path, "MY_CUSTOM=1\n", monkeypatch=monkeypatch)
    assert out.rstrip().endswith("MY_CUSTOM=1")


def test_old_file_untouched_and_out_must_differ(me, tmp_path, monkeypatch, capsys):
    out, old = _run(me, tmp_path, "BOT_TOKEN=1:a\n", monkeypatch=monkeypatch)
    assert old.read_text(encoding="utf-8") == "BOT_TOKEN=1:a\n"
    import sys
    monkeypatch.setattr(sys, "argv", ["m", "--old", str(old), "--template", str(tmp_path / ".env.example"), "--out", str(old)])
    with pytest.raises(SystemExit):
        me.main()


def test_lint_flags_bad_planner_and_showcase_prompts(me):
    bad = {
        "PLANNER_SYSTEM_PROMPT": '"Ты — Planner. Верни JSON."',
        "SHOWCASE_SYSTEM_PROMPT": '"Ты — помощник."',
    }
    text = " ".join(me.lint(bad))
    assert "PLANNER_SYSTEM_PROMPT" in text and "SHOWCASE_SYSTEM_PROMPT" in text


def test_lint_accepts_good_prompts(me):
    good = {
        "PLANNER_SYSTEM_PROMPT": '"{\\"plan\\": [call_llm respond params]}"',
        "SHOWCASE_SYSTEM_PROMPT": '"Отвечай по блоку БАЗА ЗНАНИЙ"',
    }
    assert me.lint(good) == []


def test_lint_embedding_model(me):
    assert "all-MiniLM-L6-v2" in " ".join(me.lint({"EMBEDDING_MODEL": "sentence-transformers/all-MiniLM-L6-v2"}))
    e5 = {"EMBEDDING_MODEL": "intfloat/multilingual-e5-large"}
    assert "BOT_MEM_LIMIT" in " ".join(me.lint(e5))
    assert not [w for w in me.lint({**e5, "BOT_MEM_LIMIT": "3500m", "SHOWCASE_MEM_LIMIT": "3500m"}) if "ОЗУ" in w]


def test_lint_large_corpus_chunk_with_small_window_model(me):
    assert "CORPUS_CHUNK_SIZE" in " ".join(me.lint({"CORPUS_CHUNK_SIZE": "1200"}))
    assert not [w for w in me.lint({"CORPUS_CHUNK_SIZE": "450"}) if "CORPUS_CHUNK" in w]
    big_ok = {"CORPUS_CHUNK_SIZE": "1200", "EMBEDDING_MODEL": "intfloat/multilingual-e5-large",
              "BOT_MEM_LIMIT": "3500m", "SHOWCASE_MEM_LIMIT": "3500m"}
    assert not [w for w in me.lint(big_ok) if "CORPUS_CHUNK" in w]
