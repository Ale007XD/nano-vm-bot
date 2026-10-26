"""
Инструкционные навыки формата Agent Skills (SKILL.md):
парсер frontmatter, загрузка папок, установка из присланного файла.
"""
import pytest

from app.core import skills_loader as sl

SAMPLE = """---
name: meeting_summary
description: Составляет краткое саммари встречи по её заметкам
version: 1.2.0
author: community
---

# Саммари встречи

Правила:
1. Выдели решения и ответственных.
2. Составь список задач с дедлайнами.
"""


class TestParseSkillMd:
    def test_frontmatter_and_body(self):
        parsed = sl.SkillLoader.parse_skill_md(SAMPLE)
        meta = parsed["meta"]
        assert meta["name"] == "meeting_summary"
        assert "саммари" in meta["description"].lower()
        assert "Выдели решения" in parsed["instructions"]

    def test_no_frontmatter(self):
        parsed = sl.SkillLoader.parse_skill_md("# Только инструкции\nДелай X.")
        assert parsed["meta"] == {}
        assert "Делай X." in parsed["instructions"]

    def test_frontmatter_fallback_parser(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("no yaml")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_yaml)
        parsed = sl.SkillLoader.parse_skill_md(
            "---\nname: plain\ndescription: Простое описание\n---\nТело."
        )
        assert parsed["meta"]["name"] == "plain"
        assert parsed["meta"]["description"] == "Простое описание"


class TestLoadSkillMdFolder:
    def test_load_folder(self, tmp_path):
        skill_dir = tmp_path / "meeting_summary"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(SAMPLE, encoding="utf-8")

        loader = sl.SkillLoader()
        info = loader._load_skill_md_folder(str(skill_dir), "external")

        assert info.kind == "skillmd"
        assert info.name == "meeting_summary"
        assert info.callable is None
        assert "Выдели решения" in info.instructions
        assert info.source == "external"

    def test_folder_without_instructions_raises(self, tmp_path):
        skill_dir = tmp_path / "empty"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: empty\n---\n", encoding="utf-8"
        )
        loader = sl.SkillLoader()
        with pytest.raises(ValueError, match="no instructions"):
            loader._load_skill_md_folder(str(skill_dir), "external")


class TestInstallSkillMd:
    @pytest.fixture(autouse=True)
    def _external_dir(self, tmp_path, monkeypatch):
        external = tmp_path / "external"
        external.mkdir()
        dirs = dict(sl._SKILL_DIRS)
        dirs["external"] = str(external)
        monkeypatch.setattr(sl, "_SKILL_DIRS", dirs)
        return external

    def test_install_and_register(self, _external_dir):
        loader = sl.SkillLoader()
        info = loader.install_skill_md("my-hint", SAMPLE)

        assert info.name == "meeting_summary"
        assert loader.get_skill_info("meeting_summary") is info
        assert (_external_dir / "meeting_summary" / "SKILL.md").exists()

    def test_install_sanitizes_hyphenated_name(self, _external_dir):
        """Имя папки санитизируется (дефис → _); имя навыка остаётся из frontmatter"""
        loader = sl.SkillLoader()
        hyphen = SAMPLE.replace("meeting_summary", "meeting-summary")
        info = loader.install_skill_md("hint", hyphen)
        assert info.name == "meeting-summary"
        assert (_external_dir / "meeting_summary" / "SKILL.md").exists()

    def test_install_invalid_name_sanitized(self, _external_dir):
        loader = sl.SkillLoader()
        no_name = "---\ndescription: тест\n---\nИнструкции тут."
        info = loader.install_skill_md("плохое имя!!", no_name)
        assert info.name  # санитизировано в валидное
        assert info.kind == "skillmd"

    def test_install_empty_body_raises(self, _external_dir):
        loader = sl.SkillLoader()
        with pytest.raises(ValueError, match="инструкций"):
            loader.install_skill_md("x", "---\nname: x\n---\n")
