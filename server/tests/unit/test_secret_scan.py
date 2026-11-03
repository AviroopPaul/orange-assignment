"""The scan must find a planted key, and must not print it."""

from pathlib import Path

import pytest

from scripts.scan_secrets import check_ignore_files, find_secrets, main

FAKE_KEY = "AIza" + "x" * 35


def test_each_kind_of_key_is_found() -> None:
    assert find_secrets(f"key = {FAKE_KEY}") == ["Google API key"]
    assert find_secrets("gsk_" + "a" * 30) == ["Groq API key"]
    assert find_secrets("sk-" + "a" * 40) == ["OpenAI-style key"]
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    assert find_secrets(f"{header}\n{'A' * 64}") == ["Private key"]
    assert find_secrets(f"the {header} line, in a sentence") == []


def test_the_real_key_value_is_found_whatever_it_looks_like() -> None:
    assert find_secrets("value: abc123", ("abc123",)) == ["The value of GOOGLE_API_KEY"]


def test_ordinary_text_and_a_blank_key_find_nothing() -> None:
    assert find_secrets("GOOGLE_API_KEY=", ("",)) == []


def test_a_missing_ignore_entry_is_reported(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    assert ".gitignore: does not list .env" in check_ignore_files(tmp_path)


def test_the_repo_is_clean_and_nothing_secret_is_printed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["scan_secrets.py"])
    code = main()
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert FAKE_KEY not in printed
