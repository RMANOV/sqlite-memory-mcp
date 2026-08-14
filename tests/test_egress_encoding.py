"""S0 — transport contract: egress JSON must not escape non-ASCII.

Python's ``json.dumps`` defaults to ``ensure_ascii=True``, turning each
Cyrillic character into a six-character ``\\uXXXX`` sequence. Pydantic then
doubles the backslash when embedding the tool string in the MCP envelope, so
one Cyrillic letter costs **seven** wire characters instead of one.

Measured on the live corpus: 2.31x median inflation at the real transport
boundary (``CallToolResult.model_dump_json()``).

Two invariants are asserted here, and they pull in opposite directions:

1. **Egress** (``return json.dumps(...)``) must emit raw UTF-8.
2. **Persistence** (values written into DB columns) must NOT change, or the
   column becomes a mix of escaped and unescaped rows and every byte-level
   comparison — bridge diff, hashes, dedup — silently disagrees.

``session_server.py`` holds one of each, three lines apart.

**Fixture provenance.** The measurements cited across this branch were taken on
a private corpus of real logged queries. The literals published here are
structure-preserving stand-ins — same script mix, token count and character
classes, different words. A reader cannot reproduce the exact figures without
that corpus, and could not have anyway.
"""

import ast
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

REPO = pathlib.Path(__file__).resolve().parent.parent

# Read surfaces whose return values reach an MCP client (directly or via the
# n8n connector, which lifts the same callables verbatim).
EGRESS_MODULES = [
    "server.py",
    "session_server.py",
    "entity_server.py",
    "task_server.py",
    "intel_server.py",
]

# Serialisations that are NOT egress. Changing these rewrites stored bytes.
PERSISTENCE_SITES = {
    # files_json -> sessions.active_files (INSERT :77 / UPDATE :69),
    # read back by smart_retrieval.py via json.loads.
    ("session_server.py", "session_save"),
}

# Structure matters here, not the words: Cyrillic letters, a `№` sign, an
# em-dash and a digit all take different numbers of wire characters once
# escaped. The company is fictional on purpose — see the note above.
CYRILLIC_SAMPLE = "Оборотна ведомост №5 — Примерна Фирма ООД"


def _dumps_calls(path: pathlib.Path):
    """Yield (function_name, is_return_stmt, has_ensure_ascii) per call."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                owner.setdefault(line, node.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "dumps"
            and isinstance(func.value, ast.Name)
            and func.value.id == "json"
        ):
            continue
        has_flag = any(
            kw.arg == "ensure_ascii"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.keywords
        )
        yield owner.get(node.lineno, "<module>"), node.lineno, has_flag


def _returned_dumps_lines(path: pathlib.Path) -> set[int]:
    """Line numbers of ``json.dumps`` calls that are the returned expression."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            for sub in ast.walk(node.value):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "dumps"
                ):
                    lines.add(sub.lineno)
    return lines


class TestEgressEncoding:
    def test_every_returned_dumps_disables_ascii_escaping(self):
        """Each ``return json.dumps(...)`` must carry ensure_ascii=False."""
        offenders = []
        for name in EGRESS_MODULES:
            path = REPO / name
            returned = _returned_dumps_lines(path)
            for fn, line, has_flag in _dumps_calls(path):
                if line in returned and not has_flag:
                    offenders.append(f"{name}:{line} in {fn}()")
        assert not offenders, (
            "egress json.dumps without ensure_ascii=False: " + ", ".join(offenders)
        )

    def test_persistence_sites_keep_default_escaping(self):
        """Values bound into DB columns must NOT be switched to raw UTF-8.

        A mixed column (old rows escaped, new rows raw) still parses, so this
        never raises at runtime — it only breaks byte comparisons later.
        """
        violations = []
        for name, fn_name in PERSISTENCE_SITES:
            path = REPO / name
            returned = _returned_dumps_lines(path)
            for fn, line, has_flag in _dumps_calls(path):
                if fn == fn_name and line not in returned and has_flag:
                    violations.append(f"{name}:{line} in {fn}()")
        assert not violations, (
            "persistence json.dumps must keep default escaping: "
            + ", ".join(violations)
        )


class TestSemanticEquivalence:
    """Disabling escaping changes bytes, never the parsed structure."""

    CASES = {
        "cyrillic": CYRILLIC_SAMPLE,
        "emoji": "готово ✅ грешка ❌ ракета 🚀",
        "zwj_family": "👨‍👩‍👧‍👦",
        "combining": "é vs é",
        "astral": "𝕊𝕢𝕝𝕚𝕥𝕖 𝟙𝟚𝟛",
        "cjk": "記憶 · 데이터베이스",
        "rtl": "זיכרון · ذاكرة",
        "control": 'таб\tнов ред\nкавичка" наклонена\\',
        "zero_width": "а​б‌в‍г﻿",
    }

    def test_parsed_structure_is_identical(self):
        for label, text in self.CASES.items():
            payload = {"v": text, "nested": {"list": [text, {"k": text}]}}
            escaped = json.dumps(payload)
            raw = json.dumps(payload, ensure_ascii=False)
            assert json.loads(escaped) == json.loads(raw) == payload, label

    def test_utf8_round_trip(self):
        for label, text in self.CASES.items():
            payload = {"v": text}
            raw = json.dumps(payload, ensure_ascii=False)
            assert json.loads(raw.encode("utf-8").decode("utf-8")) == payload, label

    def test_raw_form_is_smaller_for_cyrillic(self):
        payload = {"v": CYRILLIC_SAMPLE}
        escaped = json.dumps(payload)
        raw = json.dumps(payload, ensure_ascii=False)
        assert len(raw) < len(escaped)
