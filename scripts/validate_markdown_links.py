#!/usr/bin/env python3
"""Validate local Markdown paths and GitHub heading fragments (CPU-only)."""
from __future__ import annotations

import argparse
import html
from pathlib import Path
import re
import unicodedata
from urllib.parse import unquote, urlsplit

_FENCE = re.compile(r"^ {0,3}(\x60{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)\s*$")
_NAMED_ANCHOR = re.compile(r"<a\s+[^>]*?\b(?:id|name)\s*=\s*(['\"])(.*?)\1[^>]*>", re.I)
_INLINE_LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\((<[^>\n]+>|[^)\n]+)\)")
_REFERENCE_LINK = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)")
_INLINE_CODE = re.compile(r"(\x60+)(.*?)\1")
_MD_LINK_IN_HEADING = re.compile(r"!?\[([^\]]+)\]\([^)]*\)")


def rendered_lines(markdown: str):
    """Skip fenced blocks so embedded examples never create navigation IDs."""
    marker = ""
    marker_length = 0
    for number, line in enumerate(markdown.splitlines(), 1):
        found = _FENCE.match(line)
        if marker:
            if found and found.group(1)[0] == marker and len(found.group(1)) >= marker_length:
                marker = ""
            continue
        if found:
            marker, marker_length = found.group(1)[0], len(found.group(1))
            continue
        yield number, line


def heading_slug(title: str) -> str:
    """GitHub-compatible slug for the project's common Unicode/ATX headings."""
    title = _MD_LINK_IN_HEADING.sub(lambda match: match.group(1), title)
    title = _INLINE_CODE.sub(lambda match: match.group(2), title)
    title = re.sub(r"<[^>]*>", "", title)
    title = html.unescape(title)
    title = re.sub(r"\s+#+\s*$", "", title).lower()
    title = title.replace("\t", " ")
    return "".join(
        char for char in title
        if char in "-_ " or unicodedata.category(char)[0] in "LMN"
        or unicodedata.category(char) == "Pc"
    ).replace(" ", "-")


def anchors_for(markdown: str) -> set[str]:
    ids: set[str] = set()
    occurrences: dict[str, int] = {}
    for _number, line in rendered_lines(markdown):
        for match in _NAMED_ANCHOR.finditer(line):
            ids.add(html.unescape(match.group(2)))
        heading = _HEADING.match(line)
        if heading:
            slug = heading_slug(heading.group(1))
            count = occurrences.get(slug, 0)
            occurrences[slug] = count + 1
            ids.add(slug if not count else f"{slug}-{count}")
    return ids


def link_urls(markdown: str):
    for number, line in rendered_lines(markdown):
        stripped = _INLINE_CODE.sub("", line)
        for match in _INLINE_LINK.finditer(stripped):
            yield number, match.group(1)
        match = _REFERENCE_LINK.match(stripped)
        if match:
            yield number, match.group(1)


def normalize_destination(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and ">" in value:
        return html.unescape(value[1:value.index(">")])
    return html.unescape(re.split(r"\s+['\"]", value, maxsplit=1)[0])


def check_markdown_links(root: Path) -> tuple[int, int, list[str]]:
    """Return (scanned Markdown files, local fragment links, errors)."""
    root = root.resolve()
    paths = sorted(root.rglob("*.md"))
    texts: dict[Path, str] = {}
    ids: dict[Path, set[str]] = {}
    for path in paths:
        if any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue
        texts[path] = path.read_text(encoding="utf-8")
        ids[path] = anchors_for(texts[path])
    errors: list[str] = []
    fragment_count = 0
    for path, content in texts.items():
        for line_no, raw in link_urls(content):
            destination = normalize_destination(raw)
            parts = urlsplit(destination)
            if parts.scheme or parts.netloc or destination.startswith(("/", "//")):
                continue
            if not parts.path and not parts.fragment:
                continue
            target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            context = f"{path.relative_to(root)}:{line_no} -> {destination}"
            if not target.is_relative_to(root) or not target.exists():
                errors.append(f"失效文件链接: {context}")
                continue
            if parts.fragment:
                fragment_count += 1
                if target.suffix.lower() != ".md":
                    continue
                target_ids = ids.get(target)
                if target_ids is None:
                    errors.append(f"无法验证Markdown锚点: {context}")
                elif unquote(parts.fragment) not in target_ids:
                    errors.append(f"失效章节锚点: {context}")
    return len(texts), fragment_count, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    docs, fragments, errors = check_markdown_links(args.root)
    for issue in errors:
        print("MARKDOWN_LINK_FAILURE:", issue)
    if errors:
        return 1
    print(f"MARKDOWN_LINK_PASS: {docs} Markdown files, {fragments} local fragments; CPU-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
