from __future__ import annotations

import base64
from pathlib import Path

import pytest
import requests

from tbank.rest import (
    _der_to_pem,
    _read_certs,
    build_ca_bundle,
)


def _first_cert_pem() -> str:
    """Настоящий сертификат из бандла certifi — как тестовый образец PEM."""
    import certifi

    text = Path(certifi.where()).read_text(encoding="utf-8", errors="ignore")
    start = text.index("-----BEGIN CERTIFICATE-----")
    end = text.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
    return text[start:end]


def test_der_to_pem_structure():
    pem = _first_cert_pem()
    der = base64.b64decode("".join(pem.splitlines()[1:-1]))
    rebuilt = _der_to_pem(der)
    assert rebuilt.strip().startswith("-----BEGIN CERTIFICATE-----")
    assert rebuilt.strip().endswith("-----END CERTIFICATE-----")
    assert base64.b64decode("".join(rebuilt.splitlines()[1:-1])) == der


def test_read_certs_from_pem_file(tmp_path):
    f = tmp_path / "Test_Root_CA.cer"
    f.write_text(_first_cert_pem(), encoding="utf-8")
    text = _read_certs(f)
    assert "BEGIN CERTIFICATE" in text
    assert "BEGIN PRIVATE KEY" not in text  # чужие блоки не протекают


def test_read_certs_from_der_file(tmp_path):
    pem = _first_cert_pem()
    der = base64.b64decode("".join(pem.splitlines()[1:-1]))
    f = tmp_path / "binary.crt"
    f.write_bytes(der)
    text = _read_certs(f)
    assert text.strip().startswith("-----BEGIN CERTIFICATE-----")


def test_read_certs_skips_private_key(tmp_path):
    f = tmp_path / "key.pem"
    f.write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", encoding="utf-8")
    assert _read_certs(f) == ""


def test_build_bundle_merges_certifi_and_custom(tmp_path):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "Russian_Trusted_Root_CA.cer").write_text(_first_cert_pem(), encoding="utf-8")
    out = tmp_path / "data" / "ca_bundle.pem"

    bundle = build_ca_bundle(search_dirs=[certs_dir], output_path=out)
    assert bundle is not None and Path(bundle).exists()
    merged = Path(bundle).read_text(encoding="utf-8")
    assert merged.count("BEGIN CERTIFICATE") >= 2  # certifi + наш


def test_build_bundle_without_certs_returns_none(tmp_path):
    empty = tmp_path / "certs"
    empty.mkdir()
    assert build_ca_bundle(search_dirs=[empty], output_path=tmp_path / "b.pem") is None


def test_build_bundle_falls_back_to_temp_when_dir_unwritable(tmp_path):
    """Если папка недоступна для записи (файлы под root, сервис от другого юзера),
    бандл уходит во временный каталог, а бот не падает."""
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "root.cer").write_text(_first_cert_pem(), encoding="utf-8")
    # output_path под "файлом" вместо каталога -> запись неизбежно падает с OSError
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("я файл", encoding="utf-8")
    out = blocker / "data" / "ca_bundle.pem"

    bundle = build_ca_bundle(search_dirs=[certs_dir], output_path=out)
    assert bundle is not None
    import tempfile
    from pathlib import Path as _Path

    assert _Path(bundle).exists()
    assert "BEGIN CERTIFICATE" in _Path(bundle).read_text(encoding="utf-8")
    assert "tbank-bot-ca-bundle" in bundle


def test_client_uses_built_bundle(tmp_path, monkeypatch):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "root.cer").write_text(_first_cert_pem(), encoding="utf-8")
    out = tmp_path / "data" / "ca_bundle.pem"
    monkeypatch.setattr("tbank.rest._PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("TLS_INSECURE", raising=False)

    from tbank.rest import TBankRestClient

    client = TBankRestClient(token="dummy", mode="sandbox")
    assert client.session.verify == str(out)
    assert Path(client.session.verify).exists()


def test_client_insecure_flag_disables_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("TLS_INSECURE", "1")
    from tbank.rest import TBankRestClient

    client = TBankRestClient(token="dummy", mode="sandbox")
    assert client.session.verify is False
