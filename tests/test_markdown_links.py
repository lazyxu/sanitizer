"""Unit tests for Markdown file links, GitHub headings, and local fragments."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_markdown_links.py"
spec = importlib.util.spec_from_file_location("validate_markdown_links", SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class MarkdownLinksTests(unittest.TestCase):
    def test_unicode_heading(self):
        self.assertEqual(
            validator.heading_slug("2.1 XNACK 的具体功能及其与 HMM 的区别"),
            "21-xnack-的具体功能及其与-hmm-的区别",
        )

    def test_duplicate_heading_and_explicit_anchor(self):
        ids = validator.anchors_for(
            "# 主题\n## 主题\n<a id=\"case-s01\"></a>\n"
            "~~~bash\n## example-only\n~~~\n"
        )
        self.assertEqual(ids, {"主题", "主题-1", "case-s01"})

    def test_local_fragments_and_reference_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "topic.md").write_text(
                "# 第一章\n## 模型 2.0\n## 模型 2.0\n<a id=\"case-s01\"></a>\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "[章节](docs/topic.md#第一章)\n"
                "[后续](docs/topic.md#模型-20-1)\n"
                "[旧锚点](docs/topic.md#case-s01)\n"
                "[引用]: docs/topic.md#第一章\n",
                encoding="utf-8",
            )
            self.assertEqual(validator.check_markdown_links(root), (2, 4, []))

    def test_missing_file_and_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text(
                "# A\n[missing](#missing)\n[broken](ghost.md)\n", encoding="utf-8"
            )
            docs, fragments, failures = validator.check_markdown_links(root)
            self.assertEqual((docs, fragments, len(failures)), (1, 1, 2))

    def test_ignore_links_and_headings_inside_code_fences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text(
                "~~~cpp\n[example](ghost.md#foo)\n## ghost\n~~~\n"
                "[title](#title)\n## title\n",
                encoding="utf-8",
            )
            self.assertEqual(validator.check_markdown_links(root), (1, 1, []))


if __name__ == "__main__":
    unittest.main()
