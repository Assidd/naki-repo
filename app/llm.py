"""Провайдеры модели: OpenAI (основной) и Gemini (необязательный запасной).

Каждый провайдер получает system/user промпт и JSON-схему ответа, возвращает разобранный JSON
и расход токенов. Ключи читаются только на сервере из .env.
"""
import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
# Импорт на уровне модуля: тяжёлый импорт внутри обработчика блокировал сервер на первом вызове.
from openai import AsyncOpenAI, BadRequestError, OpenAIError


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    data: dict
    model: str
    tokens_in: int
    tokens_out: int
    audio_tokens: int = 0  # часть tokens_in, пришедшая на звук


def _price(name: str) -> float:
    try:
        return float(os.getenv(name, "") or 0)
    except ValueError:
        return 0.0


# Ориентировочные цены USD за 1 млн токенов (вход, выход). Сверяйте с прайсом провайдера;
# для модели по умолчанию цену можно переопределить в .env (*_PRICE_INPUT_PER_1M / *_PRICE_OUTPUT_PER_1M).
KNOWN_PRICES = {
    "gpt-6-luna": (0.10, 0.50),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "gemini-3.8-flash": (0.75, 3.75),  # вводной тариф до 31.12.2026; звук — по той же цене
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
}
# Входной звук у Gemini дороже текста (USD за 1 млн аудио-токенов, ~32 токена на секунду записи)
AUDIO_PRICES = {"gemini-3.8-flash": 0.75, "gemini-2.5-flash-lite": 0.30, "gemini-2.5-flash": 1.00, "gemini-2.5-pro": 1.25, "gemini-2.0-flash": 0.70}

DEFAULT_OPENAI_MODELS = "gpt-4o-mini,gpt-6-luna,gpt-4.1-mini,gpt-4.1,gpt-4o"


def cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int, audio_tokens: int = 0) -> float | None:
    prefix = provider.upper()
    p_in, p_out = _price(f"{prefix}_PRICE_INPUT_PER_1M"), _price(f"{prefix}_PRICE_OUTPUT_PER_1M")
    default_model = os.getenv(f"{prefix}_MODEL", "").strip()
    if not (p_in or p_out) or (default_model and not model.startswith(default_model)):
        # Цена из таблицы: самое длинное совпадающее начало названия модели
        match = max((k for k in KNOWN_PRICES if model.startswith(k)), key=len, default=None)
        if match:
            p_in, p_out = KNOWN_PRICES[match]
        elif not (default_model and model.startswith(default_model)):
            return None
    if not p_in and not p_out:
        return None
    audio_match = max((k for k in AUDIO_PRICES if model.startswith(k)), key=len, default=None)
    p_audio = AUDIO_PRICES[audio_match] if audio_match else p_in
    text_in = max(0, tokens_in - audio_tokens)
    return round(text_in / 1e6 * p_in + audio_tokens / 1e6 * p_audio + tokens_out / 1e6 * p_out, 6)


_clients: dict = {}


def _openai_client(key: str) -> AsyncOpenAI:
    # Создание клиента (SSL-контекст) занимает время и блокирует сервер — держим один на ключ.
    if key not in _clients:
        _clients[key] = AsyncOpenAI(api_key=key, timeout=120, max_retries=1)
    return _clients[key]


def warm_up() -> None:
    """Создаёт клиент заранее, при старте сервера, а не во время первой оценки."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key:
        _openai_client(key)


class OpenAIProvider:
    name = "openai"
    supports_audio = False

    def __init__(self, model: str | None = None):
        self.key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

    @property
    def available(self) -> bool:
        return bool(self.key)

    async def complete(self, system: str, user: str, schema: dict, audio: tuple[bytes, str] | None = None) -> LLMReply:
        client = _openai_client(self.key)
        kwargs = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "evaluation", "strict": True, "schema": schema}},
            temperature=0,
        )
        try:
            try:
                resp = await client.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                # Некоторые модели (reasoning) не принимают temperature — повторяем без неё.
                if "temperature" not in str(exc):
                    raise
                kwargs.pop("temperature")
                resp = await client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            raise LLMError(f"OpenAI: {type(exc).__name__}: {str(exc)[:300]}") from None
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LLMError(f"OpenAI отказался отвечать: {choice.message.refusal[:200]}")
        try:
            data = json.loads(choice.message.content or "")
        except ValueError:
            raise LLMError("OpenAI вернул некорректный JSON") from None
        usage = resp.usage
        return LLMReply(data, resp.model or self.model, usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0)


class GeminiProvider:
    name = "gemini"
    supports_audio = True  # Gemini принимает запись звонка напрямую

    def __init__(self, model: str | None = None):
        self.key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()

    @property
    def available(self) -> bool:
        return bool(self.key)

    async def complete(self, system: str, user: str, schema: dict, audio: tuple[bytes, str] | None = None) -> LLMReply:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        parts = []
        if audio:
            data, mime = audio
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode("ascii")}})
        parts.append({"text": user + "\n\nОтветь строго JSON по схеме:\n" + json.dumps(schema, ensure_ascii=False)})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }
        payload = None
        # Временные ошибки Google (перегрузка 503, лимит 429, 500) повторяем с паузой
        delays = [4, 12, 25]
        async with _gemini_slots(bool(audio)):
            for attempt in range(len(delays) + 1):
                try:
                    async with httpx.AsyncClient(timeout=300 if audio else 120) as client:
                        resp = await client.post(url, json=body, headers={"x-goog-api-key": self.key})
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < len(delays):
                        await asyncio.sleep(delays[attempt])
                        continue
                    if resp.status_code >= 400:
                        try:
                            msg = resp.json().get("error", {}).get("message", "")
                        except ValueError:
                            msg = ""
                        raise LLMError(f"Gemini: HTTP {resp.status_code}" + (f" — {msg[:200]}" if msg else ""))
                    payload = resp.json()
                    break
                except httpx.HTTPError as exc:
                    if attempt < len(delays):
                        await asyncio.sleep(delays[attempt])
                        continue
                    raise LLMError(f"Gemini: {type(exc).__name__}") from None
        try:
            parts_out = payload["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts_out if not p.get("thought"))
            data = json.loads(text)
        except (KeyError, IndexError, ValueError, TypeError):
            raise LLMError("Gemini вернул ответ без JSON") from None
        usage = payload.get("usageMetadata", {})
        audio_tokens = sum(d.get("tokenCount", 0) for d in usage.get("promptTokensDetails", []) if d.get("modality") == "AUDIO")
        out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        return LLMReply(data, self.model, usage.get("promptTokenCount", 0), out, audio_tokens)


_audio_sem: asyncio.Semaphore | None = None


@asynccontextmanager
async def _gemini_slots(is_audio: bool):
    """Запросы со звуком тяжёлые: не больше AUDIO_CONCURRENCY (по умолчанию 2) одновременно."""
    global _audio_sem
    if not is_audio:
        yield
        return
    if _audio_sem is None:
        _audio_sem = asyncio.Semaphore(int(os.getenv("AUDIO_CONCURRENCY", "2") or 2))
    async with _audio_sem:
        yield


PROVIDERS = {"openai": OpenAIProvider, "gemini": GeminiProvider}


def available_models() -> list[dict]:
    """Модели, которые можно выбрать в интерфейсе: только у провайдеров с ключом.
    Список задаётся в .env (OPENAI_MODELS / GEMINI_MODELS через запятую)."""
    out = []
    lists = {
        "openai": os.getenv("OPENAI_MODELS", "").strip() or DEFAULT_OPENAI_MODELS,
        "gemini": os.getenv("GEMINI_MODELS", "").strip() or os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
    }
    for name, cls in PROVIDERS.items():
        p = cls()
        if not p.available:
            continue
        models = [m.strip() for m in lists[name].split(",") if m.strip()]
        if p.model not in models:
            models.insert(0, p.model)
        for m in models:
            price = KNOWN_PRICES.get(max((k for k in KNOWN_PRICES if m.startswith(k)), key=len, default=""), None)
            out.append({"id": f"{name}:{m}", "provider": name, "model": m, "default": m == p.model and name == default_provider(),
                        "price": {"input_per_1m": price[0], "output_per_1m": price[1]} if price else None})
    return out


def default_provider() -> str:
    return os.getenv("LLM_PROVIDER", "openai").strip().lower()


def provider_chain(model: str | None = None) -> list:
    """Основной провайдер + запасной (если для него задан ключ).
    model — «провайдер:модель» из available_models(); тогда она становится основной."""
    primary, primary_model = default_provider(), None
    if model:
        primary, _, primary_model = model.partition(":")
    fallback = os.getenv("LLM_FALLBACK", "gemini").strip().lower()
    chain = []
    for name, m in ((primary, primary_model), (fallback, None)):
        cls = PROVIDERS.get(name)
        if cls and name not in [p.name for p in chain]:
            p = cls(m or None)
            if p.available:
                chain.append(p)
    return chain


def audio_provider(model: str | None = None):
    """Провайдер для анализа звука: выбранная модель Gemini или GEMINI_MODEL.
    None — нет ключа Gemini или анализ звука выключен (AUDIO_ANALYSIS=off)."""
    if os.getenv("AUDIO_ANALYSIS", "on").strip().lower() in ("off", "0", "false", "no"):
        return None
    name = model.partition(":")[2] if model and model.startswith("gemini:") else None
    p = GeminiProvider(name)
    return p if p.available else None


def status() -> dict:
    chain = provider_chain()
    return {
        "configured": bool(chain),
        "providers": [{"name": p.name, "model": p.model} for p in chain],
        "models": available_models(),
        "audio": {"enabled": bool(audio_provider()), "model": audio_provider().model if audio_provider() else None},
    }
