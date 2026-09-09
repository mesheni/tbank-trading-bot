"""Конфигурация бота: значения берутся из .env / переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _env_float_opt(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# Профили капитала: режимы работы под размер счёта. На малых суммах целые лоты
# MOEX не дают собрать портфель из 20%-долей (лот SBER ~3 тыс. руб, GMKN ~12 тыс.),
# поэтому позиция занимает бОльшую долю капитала, а вселенная сужается до доступных
# по лоту бумаг (фильтр по лоту — в bot при старте и cli train/backtest --capital).
CAPITAL_PROFILES: dict[str, dict] = {
    # upper_bound — верхняя граница капитала профиля, руб
    "micro": {"upper_bound": 50_000.0, "max_position_pct": 1.0, "max_total_exposure_pct": 1.0},
    "small": {"upper_bound": 200_000.0, "max_position_pct": 0.35, "max_total_exposure_pct": 0.9},
    "standard": {"upper_bound": float("inf"), "max_position_pct": 0.20, "max_total_exposure_pct": 0.8},
}

# Ниже этой суммы целые лоты почти не помещаются в бюджет позиции — торговля
# фактически невозможна (бот продолжит работать, но будет предупреждать)
MIN_TRADABLE_EQUITY_RUB = 10_000.0


def resolve_capital_profile(equity: float) -> str:
    """Имя профиля капитала по фактическому размеру счёта (для CAPITAL_PROFILE=auto)."""
    for name in ("micro", "small", "standard"):
        if equity < CAPITAL_PROFILES[name]["upper_bound"]:
            return name
    return "standard"


@dataclass
class Config:
    # --- API ---
    token: str = field(default_factory=lambda: os.getenv("T_INVEST_TOKEN", ""))
    mode: str = field(default_factory=lambda: os.getenv("MODE", "sandbox").lower())

    # --- Инструменты ---
    tickers: list[str] = field(default_factory=lambda: _env_list("TICKERS", ["SBER", "GAZP", "LKOH"]))
    candle_interval: str = field(default_factory=lambda: os.getenv("CANDLE_INTERVAL", "hour").lower())

    # --- Модель ---
    forecast_horizon: int = field(default_factory=lambda: _env_int("FORECAST_HORIZON", 1))
    history_days: int = field(default_factory=lambda: _env_int("HISTORY_DAYS", 720))

    # --- Риск-менеджмент ---
    max_position_pct: float = field(default_factory=lambda: _env_float("MAX_POSITION_PCT", 0.20))
    stop_loss_pct: float = field(default_factory=lambda: _env_float("STOP_LOSS_PCT", 0.03))
    take_profit_pct: float = field(default_factory=lambda: _env_float("TAKE_PROFIT_PCT", 0.05))
    min_abs_return: float = field(default_factory=lambda: _env_float("MIN_ABS_RETURN", 0.004))
    news_sentiment_gate: float = field(default_factory=lambda: _env_float("NEWS_SENTIMENT_GATE", -0.35))
    # во сколько раз порог выхода «прогноз развернулся» шире входного (анти-churn)
    reversal_exit_mult: float = field(default_factory=lambda: _env_float("REVERSAL_EXIT_MULT", 2.0))
    # гейт качества модели для live: новые входы только при directional_acc >= порога
    min_model_dir_acc: float = field(default_factory=lambda: _env_float("MIN_MODEL_DIR_ACC", 0.5))
    # kill-switch по просадке: при капитале ниже бюджета*(1-MAX_DRAWDOWN_PCT)
    # новые входы запрещаются (стопы/тейки продолжают работать) до перезапуска
    max_drawdown_pct: float = field(default_factory=lambda: _env_float("MAX_DRAWDOWN_PCT", 0.15))
    # лимит совокупной стоимости позиций к портфелю (none/off — без лимита)
    max_total_exposure_pct: float | None = field(
        default_factory=lambda: (
            None
            if os.getenv("MAX_TOTAL_EXPOSURE_PCT", "").strip().lower() in {"", "none", "off"}
            else _env_float("MAX_TOTAL_EXPOSURE_PCT", 0.8)
        )
    )

    # --- Издержки (для бэктеста) ---
    commission_pct: float = field(default_factory=lambda: _env_float("COMMISSION_PCT", 0.0004))
    slippage_pct: float = field(default_factory=lambda: _env_float("SLIPPAGE_PCT", 0.0002))
    # минимальная комиссия брокера за заявку, руб (0 — не учитывать)
    commission_min_rub: float = field(default_factory=lambda: _env_float("COMMISSION_MIN_RUB", 0.0))

    # --- Бот ---
    loop_interval_sec: int = field(default_factory=lambda: _env_int("LOOP_INTERVAL_SEC", 300))
    sandbox_initial_rub: float = field(default_factory=lambda: _env_float("SANDBOX_INITIAL_RUB", 1_000_000))
    # Профиль капитала: auto (определить по фактическому капиталу счёта при старте)
    # или фиксированный micro | small | standard (см. CAPITAL_PROFILES)
    capital_profile: str = field(
        default_factory=lambda: os.getenv("CAPITAL_PROFILE", "auto").strip().lower()
    )
    # бюджет счёта, руб — якорь kill-switch и P&L; по умолчанию SANDBOX_INITIAL_RUB
    # (sandbox) или фактический капитал при первом снимке портфеля (real)
    account_budget_rub: float | None = field(default_factory=lambda: _env_float_opt("ACCOUNT_BUDGET_RUB"))
    # автоперевод отклонённой лимитной заявки в рыночную; для real рекомендовано 0
    order_fallback_to_market: bool = field(default_factory=lambda: _env_bool("ORDER_FALLBACK_TO_MARKET", True))

    # --- Уведомления (email/SMTP, см. notify.py) ---
    notify_email_to: str = field(default_factory=lambda: os.getenv("NOTIFY_EMAIL_TO", ""))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 465))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.getenv("SMTP_FROM", ""))

    # --- Пути ---
    db_path: Path = field(default_factory=lambda: BASE_DIR / os.getenv("DB_PATH", "data/market.sqlite"))
    models_dir: Path = field(default_factory=lambda: BASE_DIR / os.getenv("MODELS_DIR", "models_artifacts"))
    reports_dir: Path = field(default_factory=lambda: BASE_DIR / os.getenv("REPORTS_DIR", "reports"))

    # --- NLP ---
    embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
    )
    sentiment_model: str = field(
        default_factory=lambda: os.getenv("SENTIMENT_MODEL", "Blanchefort/rubert-base-cased-sentiment")
    )
    news_half_life_hours: float = field(default_factory=lambda: _env_float("NEWS_HALF_LIFE_HOURS", 24.0))
    # Тональность: auto (трансформер, если установлен torch+transformers, иначе лексикон) |
    # lexicon (принудительно офлайн) | transformer (паднуть, если трансформер недоступен)
    nlp_sentiment: str = field(default_factory=lambda: os.getenv("NLP_SENTIMENT", "auto").lower())
    # Эмбеддеры повестки: 0 — отключить (экономит ~450 МБ RAM, кластеризация тем off)
    nlp_embedder: bool = field(
        default_factory=lambda: os.getenv("NLP_EMBEDDER", "1").strip().lower()
        not in {"0", "false", "off", "no"}
    )

    @property
    def is_sandbox(self) -> bool:
        return self.mode == "sandbox"

    @property
    def budget_rub(self) -> float:
        """Бюджет счёта: ACCOUNT_BUDGET_RUB или SANDBOX_INITIAL_RUB по умолчанию."""
        return self.account_budget_rub if self.account_budget_rub is not None else self.sandbox_initial_rub

    def apply_profile(self, equity: float) -> str:
        """Профиль капитала: подгоняет долю позиции и лимит экспозиции под размер счёта.

        Явно заданные в .env MAX_POSITION_PCT / MAX_TOTAL_EXPOSURE_PCT сильнее профиля.
        Мутирует конфиг; возвращает имя профиля (для лога и уведомления).
        """
        profile = self.capital_profile if self.capital_profile != "auto" else resolve_capital_profile(equity)
        params = CAPITAL_PROFILES[profile]
        if not os.getenv("MAX_POSITION_PCT", "").strip():
            self.max_position_pct = params["max_position_pct"]
        if not os.getenv("MAX_TOTAL_EXPOSURE_PCT", "").strip():
            self.max_total_exposure_pct = params["max_total_exposure_pct"]
        return profile

    def validate(self) -> None:
        if self.mode not in {"sandbox", "real"}:
            raise ValueError(f"MODE должен быть sandbox|real, получено: {self.mode}")
        if not self.token:
            raise ValueError(
                "T_INVEST_TOKEN не задан. Получите токен на https://www.tbank.ru/invest/open-api "
                "и укажите его в файле .env"
            )
        for name in ("stop_loss_pct", "take_profit_pct"):
            value = getattr(self, name)
            if not 0 < value < 1:
                raise ValueError(f"{name} должен быть в (0; 1), получено: {value}")
        # доля позиции допускает 1.0: профиль micro занимает весь капитал одной позицией
        if not 0 < self.max_position_pct <= 1:
            raise ValueError(f"max_position_pct должен быть в (0; 1], получено: {self.max_position_pct}")
        if self.capital_profile != "auto" and self.capital_profile not in CAPITAL_PROFILES:
            raise ValueError(
                f"CAPITAL_PROFILE должен быть auto|micro|small|standard, получено: {self.capital_profile}"
            )
        if self.account_budget_rub is not None and self.account_budget_rub <= 0:
            raise ValueError(
                f"ACCOUNT_BUDGET_RUB должен быть положительным числом, получено: {self.account_budget_rub}"
            )
        if self.commission_min_rub < 0:
            raise ValueError(
                f"COMMISSION_MIN_RUB не может быть отрицательным, получено: {self.commission_min_rub}"
            )


# Интервалы свечей: имя -> значение enum T-Invest API и максимальная глубина одного запроса
CANDLE_INTERVALS: dict[str, dict] = {
    "1min": {"enum": "CANDLE_INTERVAL_1_MIN", "chunk": 1},
    "5min": {"enum": "CANDLE_INTERVAL_5_MIN", "chunk": 7},
    "15min": {"enum": "CANDLE_INTERVAL_15_MIN", "chunk": 14},
    "hour": {"enum": "CANDLE_INTERVAL_HOUR", "chunk": 30},
    "day": {"enum": "CANDLE_INTERVAL_DAY", "chunk": 365},
}

# Часовой пояс MOEX (MSK, UTC+3)
MSK = __import__("datetime").timezone(__import__("datetime").timedelta(hours=3))
