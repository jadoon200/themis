"""Logging in to the warehouse THEMIS measures on, and failing loudly when it cannot.

What is protected, and why it matters more than it looks:

- **A warehouse THEMIS cannot read is never "nothing moved".** Every measurement query
  used to swallow its error and return nothing, so a failed login made each model absent
  on both sides — an empty delta, reported as measured and unchanged, while dbt (logging
  in its own way) had built everything fine.
- **THEMIS logs in the way dbt does.** The connection is built from dbt-trino's own
  credential classes, so LDAP, JWT, certificate and Kerberos mean here what they mean to
  dbt. Before, only a password worked.
- **Secrets in profiles are resolved, and a missing one is named.** `{{ env_var(...) }}`
  was read as literal text; dbt's renderer leaves an unset variable in place rather than
  failing, which would have reached the warehouse as a password.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from themis.execute.profiles import ProfileError, render_profile
from themis.execute.warehouse import (
    Relation,
    TrinoClient,
    WarehouseUnavailable,
    check_warehouse,
    trino_connect,
)

HOST = os.environ.get("THEMIS_TEST_TRINO_HOST", "127.0.0.1")
PORT = int(os.environ.get("THEMIS_TEST_TRINO_PORT", "8085"))


def _profile(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "trino",
        "method": "none",
        "host": "trino.internal.example",
        "port": 443,
        "user": "svc_themis",
        "database": "hive",
        "schema": "sandbox",
        "http_scheme": "https",
    }
    return {**base, **overrides}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The arguments a connection would be opened with, without opening one."""
    import trino

    seen: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(trino.dbapi, "connect", fake_connect)
    return seen


# --- secrets in profiles -----------------------------------------------------------------------


def test_env_var_in_a_profile_is_resolved_as_dbt_would(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEMIS_TEST_PW", "s3cret")
    rendered = render_profile(
        _profile(
            password="{{ env_var('THEMIS_TEST_PW') }}",
            port="{{ env_var('THEMIS_TEST_UNSET_PORT', '8443') | as_number }}",
        )
    )
    assert rendered["password"] == "s3cret"
    assert rendered["port"] == 8443


def test_an_unset_variable_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """dbt's renderer leaves it in place; that text would have been sent as a password."""
    monkeypatch.delenv("THEMIS_TEST_NEVER_SET", raising=False)
    with pytest.raises(ProfileError, match="THEMIS_TEST_NEVER_SET"):
        render_profile(_profile(password="{{ env_var('THEMIS_TEST_NEVER_SET') }}"))


# --- logging in the way dbt does ----------------------------------------------------------------


def test_ldap_logs_in_with_the_rendered_password(
    captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import trino

    monkeypatch.setenv("THEMIS_TEST_PW", "s3cret")
    trino_connect(_profile(method="ldap", password="{{ env_var('THEMIS_TEST_PW') }}"))
    assert isinstance(captured["auth"], trino.auth.BasicAuthentication)
    assert captured["http_scheme"] == "https"
    assert (captured["host"], captured["port"], captured["catalog"]) == (
        "trino.internal.example",
        443,
        "hive",
    )


def test_jwt_and_certificate_logins_are_supported(captured: dict[str, Any]) -> None:
    import trino

    trino_connect(_profile(method="jwt", jwt_token="eyJ.a.b"))
    assert isinstance(captured["auth"], trino.auth.JWTAuthentication)
    trino_connect(
        _profile(
            method="certificate",
            client_certificate="/etc/certs/svc.pem",
            client_private_key="/etc/certs/svc.key",
        )
    )
    assert isinstance(captured["auth"], trino.auth.CertificateAuthentication)


def test_a_custom_certificate_bundle_is_used_to_verify(captured: dict[str, Any]) -> None:
    """Banks run their own certificate authority; its bundle is how HTTPS verifies."""
    trino_connect(_profile(cert="/etc/ssl/bank-ca.pem"))
    assert captured["verify"] == "/etc/ssl/bank-ca.pem"


def test_a_login_that_needs_a_browser_is_refused_with_the_alternative() -> None:
    with pytest.raises(WarehouseUnavailable, match="browser"):
        trino_connect(_profile(method="oauth"))


def test_an_incomplete_profile_is_refused_rather_than_half_connected() -> None:
    profile = _profile()
    del profile["host"]
    with pytest.raises(WarehouseUnavailable):
        trino_connect(profile)


# --- a warehouse THEMIS cannot read is never "nothing moved" ------------------------------------


def test_an_unreachable_warehouse_raises_instead_of_reading_as_absent() -> None:
    """The exact failure: every query failed, and each model read as not there."""
    client = TrinoClient(catalog="hive", host="127.0.0.1", port=1)
    with pytest.raises(WarehouseUnavailable):
        client.shape(Relation(None, "main", "fct_revenue"))


def test_stage3_stops_before_building_when_it_cannot_log_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked first: a login failure found after both builds costs minutes."""
    from themis.config import Settings
    from themis.execute import runner

    (tmp_path / "dbt_project.yml").write_text("name: p\nprofile: p\n")
    (tmp_path / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: trino\n      method: none\n"
        "      host: 127.0.0.1\n      port: 1\n      user: t\n      database: hive\n"
        "      schema: main\n"
    )

    def no_build(*args: object, **kwargs: object) -> object:
        raise AssertionError("dbt ran against a warehouse THEMIS could not log in to")

    monkeypatch.setattr(runner, "run_dbt", no_build)
    result = runner.execute(
        tmp_path, base="main", head="HEAD", models=("m",), settings=Settings(), target="dev"
    )
    assert not result.ran
    assert "nothing was built" in (result.skipped_reason or "")


def test_doctor_names_a_measurement_login_that_fails(tmp_path: Path) -> None:
    from themis.config import Settings
    from themis.onboarding import _check_measurement

    (tmp_path / "dbt_project.yml").write_text("name: p\nprofile: p\n")
    (tmp_path / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: trino\n      method: ldap\n"
        "      host: 127.0.0.1\n      port: 1\n      user: t\n"
        "      password: \"{{ env_var('THEMIS_TEST_NEVER_SET') }}\"\n"
        "      database: hive\n      schema: main\n      http_scheme: https\n"
    )
    check = _check_measurement(tmp_path, Settings(), "dev")
    assert check.status == "fail"
    assert "THEMIS_TEST_NEVER_SET" in check.detail


def _trino_up() -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, PORT)) == 0


@pytest.mark.skipif(not _trino_up(), reason="no Trino on the test port")
def test_the_measurement_login_works_against_a_real_trino() -> None:
    detail = check_warehouse(
        {
            "type": "trino",
            "method": "none",
            "host": HOST,
            "port": PORT,
            "user": "themis",
            "database": "hive",
            "schema": "main",
        },
        Path("."),
    )
    assert detail == "logged in to Trino as themis"
