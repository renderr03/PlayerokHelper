import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values

from shopbot.setup_console import configure, env_text

TOKEN = "123456789:sample_token_for_local_tests_only"


class FakeConsole:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.prompts = []
        self.messages = []

    def ask(self, prompt, *, secret=False):
        self.prompts.append((prompt, secret))
        return next(self.answers)

    def write(self, text):
        self.messages.append(text)


def test_setup_asks_for_token_and_admin_hides_token_and_generates_key(tmp_path):
    console = FakeConsole([TOKEN, "42, 43,42"])
    configure(tmp_path, console)
    env = dotenv_values(tmp_path / ".env", interpolate=False)
    assert env["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert env["ADMIN_IDS"] == "42,43"
    Fernet(env["STOCK_ENCRYPTION_KEY"].encode())
    assert console.prompts[0][1] is True
    assert TOKEN not in "\n".join(console.messages)
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_reinstall_keeps_existing_key_custom_settings_and_credentials(tmp_path):
    configure(tmp_path, FakeConsole([TOKEN, "42"]))
    original = dotenv_values(tmp_path / ".env", interpolate=False)
    with (tmp_path / ".env").open("a") as stream:
        stream.write("TIMEZONE='Asia/Tokyo'\nCUSTOM_SETTING='keep-me'\n")
    configure(tmp_path, FakeConsole(["", ""]))
    env = dotenv_values(tmp_path / ".env", interpolate=False)
    assert env["STOCK_ENCRYPTION_KEY"] == original["STOCK_ENCRYPTION_KEY"]
    assert env["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert env["ADMIN_IDS"] == "42"
    assert env["TIMEZONE"] == "Asia/Tokyo"
    assert env["CUSTOM_SETTING"] == "keep-me"


def test_invalid_inputs_reprompt_without_saving_invalid_credentials(tmp_path):
    console = FakeConsole(["invalid-secret", TOKEN, "@name", "-1", "42"])
    configure(tmp_path, console)
    env = dotenv_values(tmp_path / ".env", interpolate=False)
    assert env["TELEGRAM_BOT_TOKEN"] == TOKEN and env["ADMIN_IDS"] == "42"
    assert "invalid-secret" not in (tmp_path / ".env").read_text()
    assert "invalid-secret" not in "\n".join(console.messages)


def test_cancel_does_not_overwrite_existing_env(tmp_path):
    configure(tmp_path, FakeConsole([TOKEN, "42"]))
    before = (tmp_path / ".env").read_bytes()

    class CancelledConsole(FakeConsole):
        def ask(self, prompt, *, secret=False):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        configure(tmp_path, CancelledConsole([]))
    assert (tmp_path / ".env").read_bytes() == before


def test_existing_database_without_key_is_not_assigned_a_new_key(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/shop.sqlite").touch()
    with pytest.raises(ValueError, match="База уже существует"):
        configure(tmp_path, FakeConsole([]))
    assert not (tmp_path / ".env").exists()


def test_optional_provider_settings_preserve_literal_special_characters(tmp_path):
    cookie = "token=example; custom='literal'; other=${NOT_AN_ENV_VAR}"
    console = FakeConsole([TOKEN, "42", "api'key\\${VALUE}", cookie, "Browser/1.0"])
    configure(tmp_path, console, connections=True)
    env = dotenv_values(tmp_path / ".env", interpolate=False)
    assert env["KOSELL_API_KEY"] == "api'key\\${VALUE}"
    import json

    session = json.loads((tmp_path / "secrets/playerok.json").read_text())
    assert session["cookie"] == cookie
    assert (tmp_path / "secrets/playerok.json").stat().st_mode & 0o777 == 0o600


def test_env_writer_removes_duplicate_assignments_and_keeps_comments():
    result = env_text("# Comment\nADMIN_IDS=1\nADMIN_IDS=2\n", {"ADMIN_IDS": "42"})
    assert result.count("ADMIN_IDS=") == 1
    assert "# Comment" in result
