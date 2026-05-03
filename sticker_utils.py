"""Sticker selection helpers for persona-style WeChat replies."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass


logger = logging.getLogger(__name__)

STICKER_DIR_NAME = "sticker"
STICKER_INTRODUCTION_FILE = "sticker_introduction.md"
STICKER_COOLDOWN_SECONDS = 90
MIN_STICKER_SCORE = 3
STICKER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


@dataclass(frozen=True)
class StickerRule:
    file: str
    title: str
    keywords: tuple[str, ...]


def _clean_markdown(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"[*_>#|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_field(line: str, label: str) -> str | None:
    if label not in line:
        return None
    _, value = line.split(label, 1)
    value = value.lstrip("：: ").strip()
    return _clean_markdown(value)


def _split_keywords(text: str) -> list[str]:
    text = _clean_markdown(text)
    parts = re.split(r"[\s,，、。；;：:/（）()\"“”+]+", text)
    keywords: list[str] = []
    for part in parts:
        part = part.strip()
        if len(part) >= 2:
            keywords.append(part)
    return keywords


def _dedupe_keywords(keywords: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for keyword in keywords:
        key = keyword.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(keyword)
    return tuple(deduped)


def _normalize_sticker_name(text: str) -> str:
    text = os.path.splitext(_clean_markdown(text))[0]
    return re.sub(r"[\s~～()（）?？!！]+", "", text).lower()


def _existing_sticker_files(sticker_dir: str) -> set[str]:
    if not os.path.isdir(sticker_dir):
        return set()
    return {
        name
        for name in os.listdir(sticker_dir)
        if os.path.splitext(name)[1].lower() in STICKER_EXTENSIONS
    }


def load_sticker_rules(sticker_dir: str) -> list[StickerRule]:
    """Load sticker matching rules from sticker_introduction.md."""
    existing_files = _existing_sticker_files(sticker_dir)
    if not existing_files:
        return []

    intro_path = os.path.join(sticker_dir, STICKER_INTRODUCTION_FILE)
    if not os.path.isfile(intro_path):
        logger.warning("Sticker introduction file not found: %s", intro_path)
        return _fallback_rules_from_files(existing_files)

    try:
        content = open(intro_path, "r", encoding="utf-8").read()
    except Exception as e:
        logger.warning("Failed to read sticker introduction file '%s': %s", intro_path, e)
        return _fallback_rules_from_files(existing_files)

    rules: list[StickerRule] = []
    current_title = ""
    current_file = ""
    current_text_parts: list[str] = []

    def flush_current() -> None:
        nonlocal current_title, current_file, current_text_parts
        if not current_file:
            return
        if current_file not in existing_files:
            logger.warning("Sticker listed in introduction but file is missing: %s", current_file)
            return
        stem = os.path.splitext(current_file)[0]
        keywords = [current_title, stem, *current_text_parts]
        split_keywords: list[str] = []
        for item in keywords:
            split_keywords.extend(_split_keywords(item))
        rules.append(StickerRule(
            file=current_file,
            title=current_title or stem,
            keywords=_dedupe_keywords(split_keywords),
        ))

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = re.match(r"^##\s+\d+\.\s*(.+)$", line)
        if heading:
            flush_current()
            current_title = _clean_markdown(heading.group(1))
            current_file = ""
            current_text_parts = [current_title]
            continue

        file_value = _extract_field(line, "文件名")
        if file_value:
            match = re.search(r"([^`]+?\.(?:jpg|jpeg|png|gif|webp|bmp))", file_value, re.IGNORECASE)
            current_file = os.path.basename(match.group(1).strip()) if match else os.path.basename(file_value)
            continue

        for label in ("含义", "使用场景", "心情"):
            field_value = _extract_field(line, label)
            if field_value:
                current_text_parts.append(field_value)
                break

    flush_current()

    rules = _merge_quick_index_keywords(content, rules)

    if not rules:
        logger.warning("No sticker rules parsed from %s; falling back to file names", intro_path)
        return _fallback_rules_from_files(existing_files)
    return rules


def _merge_quick_index_keywords(content: str, rules: list[StickerRule]) -> list[StickerRule]:
    if not rules:
        return rules

    by_name: dict[str, StickerRule] = {}
    for rule in rules:
        by_name[_normalize_sticker_name(rule.title)] = rule
        by_name[_normalize_sticker_name(rule.file)] = rule

    extra_by_file: dict[str, list[str]] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line or "场景" in line:
            continue
        cells = [_clean_markdown(cell) for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        scene, sticker_name = cells[0], cells[1]
        rule = by_name.get(_normalize_sticker_name(sticker_name))
        if not rule:
            continue
        extra_by_file.setdefault(rule.file, []).extend(_split_keywords(scene))

    if not extra_by_file:
        return rules

    merged: list[StickerRule] = []
    for rule in rules:
        extra = extra_by_file.get(rule.file, [])
        if extra:
            merged.append(StickerRule(
                file=rule.file,
                title=rule.title,
                keywords=_dedupe_keywords([*rule.keywords, *extra]),
            ))
        else:
            merged.append(rule)
    return merged


def _fallback_rules_from_files(files: set[str]) -> list[StickerRule]:
    rules: list[StickerRule] = []
    for file in sorted(files):
        title = os.path.splitext(file)[0]
        rules.append(StickerRule(
            file=file,
            title=title,
            keywords=_dedupe_keywords(_split_keywords(title)),
        ))
    return rules


class StickerSelector:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.sticker_dir = os.path.join(base_dir, STICKER_DIR_NAME)
        self.rules = load_sticker_rules(self.sticker_dir)
        self._last_sent: dict[str, tuple[str, float]] = {}
        logger.info("Loaded %d sticker rules from %s", len(self.rules), self.sticker_dir)

    def select(self, sender_id: str, user_text: str, assistant_text: str, skill_name: str | None) -> str | None:
        if not skill_name:
            return None
        if not os.path.isdir(self.sticker_dir):
            return None

        best_file = None
        best_score = 0
        user_haystack = user_text.lower()
        assistant_haystack = assistant_text.lower()
        for rule in self.rules:
            score = 0
            for keyword in rule.keywords:
                keyword = keyword.lower()
                if keyword in assistant_haystack:
                    score += 2
                if keyword in user_haystack:
                    score += 1
            if score > best_score:
                best_score = score
                best_file = rule.file

        if not best_file or best_score < MIN_STICKER_SCORE:
            return None

        path = os.path.join(self.sticker_dir, best_file)
        if not os.path.isfile(path):
            return None

        now = time.monotonic()
        last_file, last_at = self._last_sent.get(sender_id, ("", 0.0))
        if last_file == best_file and now - last_at < STICKER_COOLDOWN_SECONDS:
            return None
        self._last_sent[sender_id] = (best_file, now)
        return path
