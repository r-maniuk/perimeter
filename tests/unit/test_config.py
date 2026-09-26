from pathlib import Path

import pytest

from perimeter.config import DatabaseSettings, NatsSettings, SecuritySettings, load_settings


def test_secret_is_read_from_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "db"
    secret.write_text("s3cr3t\n")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    assert DatabaseSettings().password.get_secret_value() == "s3cr3t"


def test_setting_a_value_and_its_file_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "nats"
    secret.write_text("x")
    monkeypatch.setenv("NATS_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("NATS_PASSWORD", "y")
    with pytest.raises(ValueError, match="either NATS_PASSWORD or NATS_PASSWORD_FILE"):
        NatsSettings()


def test_unreadable_secret_file_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET_FILE", "/nonexistent/secret")
    with pytest.raises(ValueError, match="SESSION_SECRET_FILE"):
        SecuritySettings()


def test_plain_environment_values_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "example")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "7")
    settings = DatabaseSettings()
    assert (settings.host, settings.pool_size) == ("example", 7)


def test_allowed_origins_are_normalised() -> None:
    settings = SecuritySettings(allowed_origins=" http://a.test/ , https://b.test ,")
    assert settings.origins == frozenset({"http://a.test", "https://b.test"})


def test_groups_can_be_overridden_whole() -> None:
    settings = load_settings(database=DatabaseSettings(host="h", password="p"))
    assert settings.database.host == "h"
    assert settings.is_development is False
