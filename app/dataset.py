"""Загрузка диалогов из API организаторов и ручной импорт JSON."""
import os

import httpx

from .db import connect, dumps, now

SPEAKERS = {"client", "operator", "bot", "system"}
CHANNELS = {"agent chat", "voice call"}
LANGUAGES = {"RU", "KZ", "EN"}


class DatasetError(ValueError):
    pass


def normalize_record(raw: dict) -> dict:
    """Проверяет запись в формате организаторов и приводит её к внутреннему виду.

    Исходные метки времени сохраняются как есть. Каждой реплике назначается ID m1, m2, ...
    — по нему модель ссылается на доказательства.
    """
    if not isinstance(raw, dict):
        raise DatasetError("Запись должна быть объектом")
    cid = str(raw.get("conversation_id") or "").strip()
    if not cid:
        raise DatasetError("У записи нет conversation_id")
    channel = str(raw.get("channel") or "agent chat").strip().lower()
    if channel not in CHANNELS:
        raise DatasetError(f"{cid}: неизвестный канал «{channel}»")
    language = str(raw.get("language") or "").strip().upper()
    if language not in LANGUAGES:
        raise DatasetError(f"{cid}: неизвестный язык «{language}»")
    transcript = raw.get("transcript")
    if not isinstance(transcript, list) or not transcript:
        raise DatasetError(f"{cid}: transcript должен быть непустым массивом")
    messages = []
    for i, m in enumerate(transcript, start=1):
        if not isinstance(m, dict):
            raise DatasetError(f"{cid}: реплика {i} должна быть объектом")
        speaker = str(m.get("speaker") or "").strip().lower()
        if speaker not in SPEAKERS:
            raise DatasetError(f"{cid}: реплика {i} — неизвестная роль «{speaker}»")
        messages.append(
            {
                "id": f"m{i}",
                "timestamp": str(m.get("timestamp") or ""),
                "speaker": speaker,
                "text": str(m.get("text") or ""),
            }
        )
    audio = raw.get("audio_url") or None
    return {"id": cid, "channel": channel, "language": language, "transcript": messages, "audio_url": audio}


def extract_records(payload) -> list:
    """Принимает ответ API организаторов, объект {records: [...]} или просто массив записей."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if payload.get("isSuccess") is False:
            raise DatasetError(payload.get("error") or "API организаторов вернул ошибку")
        data = payload.get("data", payload)
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            return data["records"]
        if isinstance(payload.get("records"), list):
            return payload["records"]
    raise DatasetError("Не найден массив records")


def save_records(records: list, group_id: str) -> dict:
    normalized = [normalize_record(r) for r in records]
    if not normalized:
        raise DatasetError("Набор пуст")
    ids = [r["id"] for r in normalized]
    if len(set(ids)) != len(ids):
        raise DatasetError("В наборе повторяются conversation_id")
    created = updated = 0
    with connect() as conn:
        for r in normalized:
            exists = conn.execute("SELECT 1 FROM conversations WHERE id=?", (r["id"],)).fetchone()
            conn.execute(
                "INSERT INTO conversations(id, group_id, channel, language, transcript, audio_url, imported_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET group_id=excluded.group_id, "
                "channel=excluded.channel, language=excluded.language, transcript=excluded.transcript, "
                "audio_url=excluded.audio_url, imported_at=excluded.imported_at",
                (r["id"], group_id, r["channel"], r["language"], dumps(r["transcript"]), r["audio_url"], now()),
            )
            if exists:
                updated += 1
            else:
                created += 1
    return {"group_id": group_id, "total": len(normalized), "created": created, "updated": updated}


async def fetch_from_organizer(group_id: str) -> dict:
    url = os.getenv("ORGANIZER_API_URL", "").strip()
    if not url:
        raise DatasetError("Не задан ORGANIZER_API_URL в файле .env. Скопируйте ссылку GET из методички.")
    if not group_id.strip():
        raise DatasetError("Укажите group_id")
    endpoint = httpx.URL(url).copy_set_param("group_id", group_id.strip())
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(endpoint)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise DatasetError(f"Не удалось получить данные организаторов: {type(exc).__name__}") from None
    except ValueError:
        raise DatasetError("API организаторов вернул не JSON") from None
    return save_records(extract_records(payload), group_id.strip())
