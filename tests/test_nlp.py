from __future__ import annotations

import pandas as pd
import pytest

from nlp.agenda import batch_score_news, score_agenda
from nlp.embedder import NewsEmbedder
from nlp.sentiment import LexiconSentiment, make_sentiment


@pytest.fixture
def sentiment():
    return LexiconSentiment()


def test_lexicon_positive_negative(sentiment):
    assert sentiment.score("Прибыль компании выросла, дивиденды повышены, рекордный год") > 0.3
    assert sentiment.score("Акции обвалились: убыток, санкции и риск дефолта") < -0.3
    assert sentiment.score("Компания опубликовала отчет за квартал") == 0.0
    assert sentiment.score("") == 0.0


def test_score_range(sentiment):
    for text in ("рост рост рост", "обвал санкции арест штраф иск суд", "нейтральный текст"):
        assert -1.0 <= sentiment.score(text) <= 1.0


def _news_df(rows):
    now = pd.Timestamp("2026-09-03 12:00", tz="UTC")
    return pd.DataFrame(
        [
            {"news_id": f"n{i}", "instrument_uid": "X", "pub_time": (now - pd.Timedelta(hours=h)).isoformat(),
             "title": t, "text": "", "pub_dt": now - pd.Timedelta(hours=h)}
            for i, (h, t) in enumerate(rows)
        ]
    )


def test_score_agenda_recent_positive(sentiment):
    now = pd.Timestamp("2026-09-03 12:00", tz="UTC")
    df = _news_df([(1, "Прибыль выросла, рекорд"), (2, "Дивиденды повышены")])
    scores = batch_score_news(df, sentiment)
    agenda = score_agenda(df, scores, now, window_hours=48)
    assert agenda.sentiment > 0
    assert agenda.n_items == 2


def test_score_agenda_recency_decay():
    now = pd.Timestamp("2026-09-03 12:00", tz="UTC")
    df = _news_df([(1, "Свежая новость"), (47, "Старая новость")])
    # свежий позитив + старый негатив даёт больший скор, чем наоборот
    positive_fresh = score_agenda(df, {"n0": 1.0, "n1": -1.0}, now, window_hours=48)
    negative_fresh = score_agenda(df, {"n0": -1.0, "n1": 1.0}, now, window_hours=48)
    assert positive_fresh.sentiment > negative_fresh.sentiment


def test_score_agenda_empty_and_window():
    now = pd.Timestamp("2026-09-03 12:00", tz="UTC")
    empty = score_agenda(pd.DataFrame(), {}, now)
    assert empty.sentiment == 0.0 and empty.n_items == 0
    # новость за пределами окна не учитывается
    old = _news_df([(100, "Очень старая новость")])
    assert score_agenda(old, {"n0": 0.5}, now, window_hours=48).n_items == 0


def test_lexicon_fallback_when_forced():
    """NLP_SENTIMENT=lexicon принудительно даёт лексикон, даже если трансформеры установлены."""
    sentiment = make_sentiment(preference="lexicon")
    assert isinstance(sentiment, LexiconSentiment)


def test_embedder_disabled_without_loading():
    """NLP_EMBEDDER=0: эмбеддер недоступен, ничего не загружается (важно для VPS с 1 ГБ RAM)."""
    embedder = NewsEmbedder(enabled=False)
    assert embedder.available is False
    assert embedder.encode(["тест"]) is None


def test_momentum():
    now = pd.Timestamp("2026-09-03 12:00", tz="UTC")
    df = _news_df([(1, "Рост прибыли"), (40, "Обвал и убытки")])
    s = {"n0": 1.0, "n1": -1.0}
    agenda = score_agenda(df, s, now, window_hours=48)
    assert agenda.momentum > 0  # свежие новости позитивнее старых


def test_macro_news_weighted_lower():
    """Макро-новости (instrument_uid='') весят вдвое меньше привязанных к тикеру."""
    now = pd.Timestamp("2026-09-03 12:00", tz="UTC")
    df = _news_df([(1, "Позитив тикера"), (1, "Негатив макро")])
    df["instrument_uid"] = ["U1", ""]
    s = {"n0": 1.0, "n1": -1.0}
    agenda = score_agenda(df, s, now, window_hours=48)
    # 1*1.0 + 0.5*(-1.0) против 1*1.0 + 1*(-1.0): с весом макро скор положительнее
    assert agenda.sentiment > 0.0
