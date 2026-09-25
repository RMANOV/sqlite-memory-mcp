"""git status --porcelain quotes paths with spaces or non-ASCII in C style.

The readiness preflight compared the quoted string ("attachments/....tmp")
against the generated-path allowlist, so an interrupted attachment write with
a space or Cyrillic in its name blocked every sync. Incident 2026-09-25 14:35:
0-byte "...ПРЕДВАРИТЕЛНА ИНФОРМАЦИЯ -  ПРОФИЛАКТИКА.docx.tmp".
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_utils  # noqa: E402

NAME = "18315b85__ПРЕДВАРИТЕЛНА ИНФОРМАЦИЯ -  ПРОФИЛАКТИКА.docx"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _bridge_repo(tmp_path, quotepath):
    repo = tmp_path / "bridge"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "core.quotepath", quotepath)
    (repo / "shared.json").write_text("{}", encoding="utf-8")
    att = repo / "attachments" / "cb00e085"
    att.mkdir(parents=True)
    (att / NAME).write_bytes(b"docx")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo, att


def test_unquote_octal_and_raw_forms():
    octal = r'"attachments/a/\320\237 x.tmp"'
    raw = '"attachments/a/П x.tmp"'
    assert db_utils._unquote_git_path(octal) == "attachments/a/П x.tmp"
    assert db_utils._unquote_git_path(raw) == "attachments/a/П x.tmp"
    assert db_utils._unquote_git_path("shared.json") == "shared.json"
    assert db_utils._unquote_git_path(r'"a\"b\\c"') == r'a"b\c'


def test_status_path_of_quoted_line():
    line = r'?? "attachments/a/\320\237 x.docx.tmp"'
    assert db_utils._bridge_status_path(line) == "attachments/a/П x.docx.tmp"


def _assert_tmp_does_not_block(tmp_path, quotepath):
    repo, att = _bridge_repo(tmp_path, quotepath)
    (att / (NAME + ".tmp")).write_bytes(b"")

    ok, msg = db_utils.ensure_bridge_repo_ready(str(repo))

    assert ok is True, msg
    assert not (att / (NAME + ".tmp")).exists()
    assert (att / NAME).read_bytes() == b"docx"


def test_empty_attachment_tmp_with_spaces_and_cyrillic_does_not_block(tmp_path):
    _assert_tmp_does_not_block(tmp_path, "false")


def test_same_with_default_octal_quoting(tmp_path):
    _assert_tmp_does_not_block(tmp_path, "true")


def test_user_file_with_spaces_still_blocks(tmp_path):
    repo, _ = _bridge_repo(tmp_path, "false")
    (repo / "бележки на Руслан.txt").write_text("user work", encoding="utf-8")

    ok, msg = db_utils.ensure_bridge_repo_ready(str(repo))

    assert ok is False
    assert "бележки на Руслан.txt" in msg
    assert (repo / "бележки на Руслан.txt").exists()
