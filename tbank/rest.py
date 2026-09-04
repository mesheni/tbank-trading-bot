"""Тонкий HTTP-клиент к REST-шлюзу T-Invest API.

T-Invest API — это gRPC-контракт, но у него есть первоклассный REST-шлюз
(https://invest-public-api.tbank.ru/rest/<Service>/<Method>), зеркально
повторяющий proto-методы. Клиент ниже использует именно его: это снимает
зависимость от grpcio и работает на любой версии Python.

Прод:   https://invest-public-api.tbank.ru
Sandbox: https://sandbox-invest-public-api.tbank.ru

TLS-сертификаты: при доступе из РФ цепочка часто подписана корнем НУЦ Минцифры,
которого нет в certifi. Клиент автоматически находит .pem/.crt/.cer файлы
в корне проекта и папке certs/ (или по env T_INVEST_CA_BUNDLE), конвертирует DER
в PEM при необходимости и собирает общий бандл с certifi.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

PROD_HOST = "https://invest-public-api.tbank.ru"
SANDBOX_HOST = "https://sandbox-invest-public-api.tbank.ru"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class APIError(RuntimeError):
    """Ошибка ответа T-Invest API."""

    def __init__(self, status: int, message: str, details: str = ""):
        super().__init__(f"HTTP {status}: {message} {details}".strip())
        self.status = status
        self.message = message
        self.details = details
_CERT_SUFFIXES = (".pem", ".crt", ".cer")
_PEM_CERT_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)


def _der_to_pem(der: bytes) -> str:
    """DER-сертификат -> PEM-текст (base64 в обёртке BEGIN/END)."""
    b64 = base64.encodebytes(der).decode("ascii")
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(b64.splitlines()) + "\n-----END CERTIFICATE-----\n"


def _read_certs(path: Path) -> str:
    """Достаёт из файла только PEM-блоки сертификатов; DER конвертирует в PEM."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("Не удалось прочитать сертификат %s: %s", path, exc)
        return ""
    if b"BEGIN CERTIFICATE" in raw:
        blocks = _PEM_CERT_RE.findall(raw.decode("utf-8", errors="ignore"))
        return "\n".join(block.strip() for block in blocks) + "\n"
    if raw[:1] == b"\x30":  # ASN.1 SEQUENCE — бинарный DER
        return _der_to_pem(raw)
    log.warning("%s не похож на сертификат (нет PEM-блоков и DER-заголовка)", path)
    return ""


def _iter_cert_files(search_dirs: list[Path]) -> list[Path]:
    explicit = os.getenv("T_INVEST_CA_BUNDLE")
    if explicit:
        path = Path(explicit)
        if path.is_dir():
            return sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in _CERT_SUFFIXES)
        return [path]
    files: list[Path] = []
    for d in search_dirs:
        if d.is_dir():
            files.extend(p for p in sorted(d.iterdir()) if p.is_file() and p.suffix.lower() in _CERT_SUFFIXES)
    return files


def build_ca_bundle(
    search_dirs: list[Path] | None = None,
    output_path: Path | None = None,
) -> str | None:
    """Собирает объединённый CA-бандл: certifi + сертификаты из проекта/env.

    Возвращает путь к бандлу или None, если дополнительных сертификатов нет
    (тогда используется стандартный бандл certifi).
    """
    try:
        import certifi
    except ImportError:  # requests без certifi — нестандартная сборка
        log.warning("certifi не найден — объединённый бандл не собирается")
        return None

    search_dirs = search_dirs or [_PROJECT_ROOT, _PROJECT_ROOT / "certs"]
    output_path = output_path or _PROJECT_ROOT / "data" / "ca_bundle.pem"

    extra = ""
    used: list[Path] = []
    for f in _iter_cert_files(search_dirs):
        text = _read_certs(f)
        if text:
            extra += text
            used.append(f.name)
    if not extra:
        return None

    base = Path(certifi.where()).read_text(encoding="utf-8", errors="ignore")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(base.rstrip() + "\n" + extra, encoding="utf-8")
    log.info("TLS: использую бандл %s (certifi + %s)", output_path, ", ".join(used))
    return str(output_path)


class TBankRestClient:
    """HTTP-транспорт с авторизацией, троттлингом и ретраями на 429/5xx."""

    def __init__(
        self,
        token: str,
        mode: str = "sandbox",
        timeout: float = 30.0,
        min_request_interval: float = 0.25,
        max_retries: int = 3,
    ):
        if not token:
            raise ValueError("Пустой токен T_INVEST_TOKEN")
        if mode not in {"sandbox", "real"}:
            raise ValueError(f"Неизвестный режим: {mode}")
        self.host = SANDBOX_HOST if mode == "sandbox" else PROD_HOST
        self.timeout = timeout
        self.max_retries = max_retries
        self._min_interval = min_request_interval
        self._last_request_ts = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # x-app-name смягчает лимиты API (см. документацию Т-Банка)
                "x-app-name": "tbank-trading-bot",
            }
        )
        self._configure_tls()

    def _configure_tls(self) -> None:
        if os.getenv("TLS_INSECURE", "").strip().lower() in {"1", "true", "yes"}:
            log.warning(
                "TLS_INSECURE=1: проверка сертификатов ОТКЛЮЧЕНА. Токен может быть "
                "перехвачен MITM — используйте только для разовой диагностики!"
            )
            self.session.verify = False
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except ImportError:
                pass
            return
        bundle = build_ca_bundle()
        self.session.verify = bundle if bundle else True

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def post(self, service_method: str, payload: dict | None = None) -> dict:
        """POST /rest/<Service>/<Method>. Возвращает JSON-ответ или {}."""
        url = f"{self.host}/rest/{service_method}"
        body = payload or {}
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.post(url, data=json.dumps(body), timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Сетевая ошибка %s (попытка %d/%d): %s", service_method, attempt, self.max_retries, exc)
                time.sleep(1.5 * attempt)
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", "1"))
                log.warning("Лимит запросов (429), пауза %.1fs", retry_after)
                time.sleep(retry_after)
                last_error = APIError(429, "rate limit")
                continue
            if resp.status_code >= 500:
                log.warning("Сервер %s -> %d (попытка %d)", service_method, resp.status_code, attempt)
                time.sleep(1.5 * attempt)
                last_error = APIError(resp.status_code, "server error")
                continue
            if resp.status_code >= 400:
                details = resp.text[:500]
                try:
                    err = resp.json()
                    details = json.dumps(err.get("error", err), ensure_ascii=False)[:500]
                except (ValueError, AttributeError):
                    pass
                raise APIError(resp.status_code, "client error", details)
            if not resp.content:
                return {}
            return resp.json()

        raise last_error or APIError(0, f"не удалось выполнить {service_method}")
