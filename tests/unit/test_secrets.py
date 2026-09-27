"""Deployment secrets: layout, permissions, idempotency and the hashes the servers receive."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
import structlog

from perimeter.tools import secrets as deployment
from perimeter.tools.secrets import (
    LAYOUT,
    NATS_USERS,
    SECRETS,
    Groups,
    NatsPasswords,
    Report,
    SecretsError,
    nats_password_secret,
    parse_nats_passwords,
    reconcile,
    scram_matches,
    scram_sha256,
)

SERVICES = ("init", "api", "engine", "generator")


@pytest.fixture
def groups() -> Groups:
    gid = os.getgid()
    return Groups(postgres=gid, nats=gid, own=gid)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "secrets"
    path.mkdir()
    return path


def _text(root: Path, relative: str) -> str:
    return (root / relative).read_text()


def _mode(root: Path, relative: str) -> int:
    return stat.S_IMODE((root / relative).stat().st_mode)


def _snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            path.stat().st_ino,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _make_writable(path: Path) -> None:
    path.chmod(0o600)


def test_first_run_generates_every_value_and_lays_out_each_service(
    root: Path, groups: Groups
) -> None:
    report = reconcile(root, groups)

    assert report.generated == [secret.name for secret in SECRETS]
    assert sorted(entry.name for entry in root.iterdir()) == sorted(
        ["store", *(consumer.name for consumer in LAYOUT)]
    )
    assert sorted(os.listdir(root / "api")) == [
        "database_password",
        "ingest_token",
        "nats_password",
        "session_secret",
    ]
    assert sorted(os.listdir(root / "engine")) == ["database_password", "nats_password"]
    assert sorted(os.listdir(root / "init")) == ["database_password", "nats_password"]
    assert os.listdir(root / "generator") == ["ingest_token"]
    assert os.listdir(root / "nats") == ["passwords.conf"]
    assert len(_text(root, "api/session_secret")) == 64  # 48 random bytes, URL-safe base64


def test_services_share_a_value_only_where_they_must(root: Path, groups: Groups) -> None:
    reconcile(root, groups)

    assert _text(root, "api/database_password") == _text(root, "engine/database_password")
    assert _text(root, "init/database_password") != _text(root, "api/database_password")
    assert _text(root, "api/ingest_token") == _text(root, "generator/ingest_token")
    broker_passwords = {_text(root, f"{user}/nats_password") for user in NATS_USERS}
    assert len(broker_passwords) == len(NATS_USERS)


def test_the_database_and_broker_receive_hashes_never_service_passwords(
    root: Path, groups: Groups
) -> None:
    reconcile(root, groups)
    server_side = "".join(
        path.read_text() for directory in ("db", "nats") for path in (root / directory).iterdir()
    )

    for service in SERVICES:
        for path in (root / service).iterdir():
            assert path.read_text() not in server_side
    assert scram_matches(_text(root, "db/perimeter.scram"), _text(root, "init/database_password"))
    assert scram_matches(
        _text(root, "db/perimeter_app.scram"), _text(root, "engine/database_password")
    )
    hashes = parse_nats_passwords(_text(root, "nats/passwords.conf"))
    assert sorted(hashes) == ["API_PASSWORD", "ENGINE_PASSWORD", "INIT_PASSWORD"]
    for user in NATS_USERS:
        password = _text(root, f"{user}/nats_password").encode()
        assert bcrypt.checkpw(password, hashes[f"{user.upper()}_PASSWORD"].encode())


def test_files_are_readable_only_by_their_service(root: Path, groups: Groups) -> None:
    reconcile(root, groups)

    assert _mode(root, "store") == 0o700
    assert {_mode(root, f"store/{secret.name}") for secret in SECRETS} == {0o400}
    for shared in ("db", "nats"):
        assert _mode(root, shared) == 0o750
        assert {_mode(root, f"{shared}/{name}") for name in os.listdir(root / shared)} == {0o440}
    for service in SERVICES:
        assert _mode(root, service) == 0o700
        assert {_mode(root, f"{service}/{name}") for name in os.listdir(root / service)} == {0o400}


def test_each_reader_gets_its_own_group(root: Path) -> None:
    others = [gid for gid in os.getgroups() if gid != os.getgid()]
    if len(others) < 2:
        pytest.skip("needs a user with two supplementary groups")
    groups = Groups(postgres=others[0], nats=others[1], own=os.getgid())

    reconcile(root, groups)

    assert {(root / "db").stat().st_gid, (root / "db" / "superuser_password").stat().st_gid} == {
        others[0]
    }
    assert (root / "nats" / "passwords.conf").stat().st_gid == others[1]
    assert (root / "api" / "session_secret").stat().st_gid == os.getgid()


def test_a_second_run_changes_nothing(root: Path, groups: Groups) -> None:
    reconcile(root, groups)
    before = _snapshot(root)

    assert reconcile(root, groups) == Report()
    assert _snapshot(root) == before


def test_missing_or_tampered_files_are_restored_with_the_same_values(
    root: Path, groups: Groups
) -> None:
    reconcile(root, groups)
    token = _text(root, "api/ingest_token")
    broker_password = _text(root, "engine/nats_password")
    (root / "api" / "ingest_token").unlink()
    for tampered in (root / "engine" / "nats_password", root / "nats" / "passwords.conf"):
        _make_writable(tampered)
        tampered.write_text("tampered\n")

    report = reconcile(root, groups)

    assert report.generated == []
    assert sorted(report.written) == [
        "api/ingest_token",
        "engine/nats_password",
        "nats/passwords.conf",
    ]
    assert _text(root, "api/ingest_token") == token
    assert _text(root, "engine/nats_password") == broker_password
    assert parse_nats_passwords(_text(root, "nats/passwords.conf"))


def test_drifted_permissions_are_repaired(root: Path, groups: Groups) -> None:
    reconcile(root, groups)
    (root / "api" / "session_secret").chmod(0o644)
    (root / "db").chmod(0o755)

    report = reconcile(root, groups)

    assert sorted(report.repaired) == ["api/session_secret", "db"]
    assert (_mode(root, "api/session_secret"), _mode(root, "db")) == (0o400, 0o750)


def test_a_value_behind_existing_service_files_is_never_regenerated(
    root: Path, groups: Groups
) -> None:
    reconcile(root, groups)
    (root / "store" / "database_app_password").unlink()

    with pytest.raises(SecretsError, match=r"api/database_password.*refusing"):
        reconcile(root, groups)
    assert not (root / "store" / "database_app_password").exists()


def test_a_lost_store_is_not_silently_replaced(root: Path, groups: Groups) -> None:
    reconcile(root, groups)
    shutil.rmtree(root / "store")

    with pytest.raises(SecretsError, match="refusing to issue credentials"):
        reconcile(root, groups)
    assert not (root / "store").exists()


@pytest.mark.parametrize(
    ("content", "message"), [(b"\n", "empty or malformed"), (b"\xff\xfe", "not ASCII")]
)
def test_a_corrupt_value_is_reported_not_replaced(
    root: Path, groups: Groups, content: bytes, message: str
) -> None:
    reconcile(root, groups)
    corrupt = root / "store" / "session_secret"
    _make_writable(corrupt)
    corrupt.write_bytes(content)

    with pytest.raises(SecretsError, match=message):
        reconcile(root, groups)


def test_symbolic_links_are_refused(root: Path, groups: Groups, tmp_path: Path) -> None:
    reconcile(root, groups)
    (tmp_path / "elsewhere").write_text("attacker controlled")
    link = root / "api" / "ingest_token"
    link.unlink()
    link.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(SecretsError, match="cannot read"):
        reconcile(root, groups)

    link.unlink()
    shutil.rmtree(root / "engine")
    (root / "engine").symlink_to(tmp_path)
    with pytest.raises(SecretsError, match="not a directory"):
        reconcile(root, groups)


def test_the_volume_must_be_mounted(tmp_path: Path, groups: Groups) -> None:
    with pytest.raises(SecretsError, match="mount the secrets volume"):
        reconcile(tmp_path / "missing", groups)


def test_leftovers_of_an_interrupted_run_are_swept(root: Path, groups: Groups) -> None:
    reconcile(root, groups)
    leftover = root / "api" / ".ingest_token.0a1b2c3d.tmp"
    leftover.write_text("half-written")

    reconcile(root, groups)

    assert not leftover.exists()


def test_scram_verifier_follows_rfc_7677() -> None:
    """The verifier authenticates the RFC's example exchange exactly as PostgreSQL would."""
    verifier = scram_sha256("pencil", salt=base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ=="))
    stored_key, server_key = (
        base64.b64decode(key) for key in verifier.rsplit("$", 1)[1].split(":")
    )
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth_message = (
        f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
        f"c=biws,r={nonce}"
    ).encode()
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    signature = hmac.digest(stored_key, auth_message, "sha256")
    client_key = bytes(a ^ b for a, b in zip(proof, signature, strict=True))

    assert verifier.startswith("SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$")
    assert hashlib.sha256(client_key).digest() == stored_key
    assert base64.b64encode(hmac.digest(server_key, auth_message, "sha256")) == (
        b"6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    )


def test_scram_verifier_accepts_only_its_own_password() -> None:
    verifier = scram_sha256("correct-horse")

    assert scram_matches(verifier, "correct-horse")
    assert not scram_matches(verifier, "correct-horse-battery")
    assert not scram_matches("md5f00d", "correct-horse")
    assert scram_sha256("correct-horse") != verifier  # salted


def test_broker_hashes_are_rewritten_when_the_user_set_changes() -> None:
    values = {nats_password_secret(user): f"password-of-{user}" for user in NATS_USERS}
    three_users = NatsPasswords(NATS_USERS).render(values)

    assert NatsPasswords(NATS_USERS).current(three_users, values)
    assert not NatsPasswords(("init", "api")).current(three_users, values)
    assert not NatsPasswords(NATS_USERS).current(
        three_users, {**values, nats_password_secret("api"): "rotated"}
    )
    assert parse_nats_passwords("# comment only\n") == {}
    assert parse_nats_passwords('API_PASSWORD: "plaintext"\n') == {}


@pytest.fixture
def isolated_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``main`` sets the umask and configures logging for the whole process; undo both."""
    monkeypatch.setattr(deployment, "configure_logging", lambda **_: None)
    umask = os.umask(0o022)
    os.umask(umask)
    yield
    os.umask(umask)


@pytest.mark.usefixtures("isolated_process")
def test_the_job_logs_what_it_did_and_never_a_value(root: Path) -> None:
    gid = str(os.getgid())
    arguments = [str(root), "--postgres-gid", gid, "--nats-gid", gid]

    with structlog.testing.capture_logs() as first:
        assert deployment.main(arguments) == 0
    with structlog.testing.capture_logs() as second:
        assert deployment.main(arguments) == 0

    output = json.dumps([*first, *second])
    for secret in SECRETS:
        assert _text(root, f"store/{secret.name}") not in output
    assert [entry["event"] for entry in first].count("secrets.ingest_token_location") == 1
    assert [entry["event"] for entry in second] == ["secrets.ready"]


@pytest.mark.usefixtures("isolated_process")
def test_the_job_fails_loudly(tmp_path: Path) -> None:
    with structlog.testing.capture_logs() as logs:
        assert deployment.main([str(tmp_path / "missing")]) == 1
    assert logs[-1]["event"] == "secrets.failed"
