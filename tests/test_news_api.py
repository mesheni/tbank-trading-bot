"""Тесты нормализации новостного ответа T-Invest API (по реальной структуре)."""
from __future__ import annotations

from tbank.api import TBankAPI


def test_normalize_real_structure():
    """Структура из реального ответа InstrumentsService/News (2026-09)."""
    raw = {
        "id": "109791155",
        "source": "tass",
        "title": "Заголовок новости",
        "content": "Текст новости",
        "tables": [],
        "instrumentId": [
            {"instrument": {"instrumentUid": "e6123145-9665-43e0-8413-cd61b8aa9b13", "ticker": "SBER", "classCode": "TQBR"}},
            {"instrument": {"instrumentUid": "aac2b935-3d94-4030-83a1-f7acdd9b05a5", "ticker": "DOMRF", "classCode": "TQBR"}},
        ],
        "priority": False,
        "ts": "2026-09-03T21:21:57.111Z",
    }
    item = TBankAPI._normalize_news(raw)
    assert item["news_id"] == "109791155"
    assert item["pub_time"] == "2026-09-03T21:21:57.111Z"
    assert item["title"] == "Заголовок новости"
    assert item["text"] == "Текст новости"
    assert item["instrument_uid"] == "e6123145-9665-43e0-8413-cd61b8aa9b13"
    assert item["instrument_uids"] == ["e6123145-9665-43e0-8413-cd61b8aa9b13", "aac2b935-3d94-4030-83a1-f7acdd9b05a5"]


def test_normalize_macro_news_without_instruments():
    raw = {
        "id": "109791162",
        "source": "tass",
        "title": "Макро-новость",
        "content": "Текст без привязки",
        "instrumentId": [],
        "ts": "2026-09-03T21:40:43Z",
    }
    item = TBankAPI._normalize_news(raw)
    assert item["news_id"] == "109791162"
    assert item["instrument_uid"] == ""
    assert item["instrument_uids"] == []


def test_normalize_legacy_field_names():
    raw = {
        "newsId": "42",
        "pubTime": "2026-09-01T10:00:00Z",
        "title": "Старый формат",
        "text": "текст",
        "instrumentUid": "abc",
    }
    item = TBankAPI._normalize_news(raw)
    assert item["news_id"] == "42"
    assert item["pub_time"] == "2026-09-01T10:00:00Z"
    assert item["text"] == "текст"
    assert item["instrument_uids"] == ["abc"]
