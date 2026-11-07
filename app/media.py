"""Записи звонков: получение прямой ссылки на mp3 и загрузка аудио для анализа моделью.

audio_url в данных организаторов ведёт на HTML-страницу с плеером; в ней лежит временная
(около часа) ссылка на файл. Загружаем только со страниц сервера организаторов.
"""
import html
import os
import re
import time
from urllib.parse import urlsplit

import httpx

MAX_AUDIO_BYTES = 18 * 1024 * 1024  # inline-лимит запроса Gemini — 20 МБ на весь запрос
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")
_cache: dict[str, tuple[float, str]] = {}


class MediaError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _is_direct(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(AUDIO_EXT)


async def resolve_audio(cid: str, page_url: str | None) -> str:
    """Прямая HTTPS-ссылка на аудиофайл звонка."""
    if not page_url:
        raise MediaError("У диалога нет записи", 404)
    cached = _cache.get(cid)
    if cached and cached[0] > time.time():
        return cached[1]
    if _is_direct(page_url):
        return page_url
    allowed = urlsplit(os.getenv("ORGANIZER_API_URL", "")).hostname
    if not allowed or (urlsplit(page_url).hostname or "") != allowed:
        raise MediaError("Запись не с сервера организаторов — откройте ссылку вручную", 400)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(page_url)
            resp.raise_for_status()
    except httpx.HTTPError:
        raise MediaError("Не удалось открыть плеер организаторов") from None
    match = re.search(r'<source[^>]+src="([^"]+)"', resp.text) or re.search(r'<audio[^>]+src="([^"]+)"', resp.text)
    if not match:
        raise MediaError("На странице плеера не найдена ссылка на запись")
    src = html.unescape(match.group(1))
    if urlsplit(src).scheme != "https":
        raise MediaError("Ссылка на запись не HTTPS")
    _cache[cid] = (time.time() + 40 * 60, src)
    return src


def forget(cid: str) -> None:
    _cache.pop(cid, None)


async def download_audio(cid: str, page_url: str | None) -> tuple[bytes, str]:
    """Скачивает запись целиком (для отправки модели). Возвращает (байты, MIME-тип)."""
    src = await resolve_audio(cid, page_url)
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", src) as resp:
                resp.raise_for_status()
                mime = (resp.headers.get("content-type") or "audio/mpeg").split(";")[0].strip()
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise MediaError("Запись больше 18 МБ — анализ звука пропущен")
                    chunks.append(chunk)
    except httpx.HTTPError:
        forget(cid)  # временная ссылка могла истечь — в следующий раз получим новую
        raise MediaError("Не удалось скачать запись звонка") from None
    if not mime.startswith("audio/"):
        mime = "audio/mpeg"
    return b"".join(chunks), mime
