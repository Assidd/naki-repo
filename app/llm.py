"""Провайдеры модели: OpenAI (основной) и Gemini (необязательный запасной).

Каждый провайдер получает system/user промпт и JSON-схему ответа, возвращает разобранный JSON
и расход токенов. Ключи читаются только на сервере из .env.
"""
import json
import os
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


def _price(name: str) -> float:
    try:
        return float(os.getenv(name, "") or 0)
    except ValueError:
        return 0.0


def cost_usd(provider: str, tokens_in: int, tokens_out: int) -> float | None:
    prefix = provider.upper()
    p_in, p_out = _price(f"{prefix}_PRICE_INPUT_PER_1M"), _price(f"{prefix}_PRICE_OUTPUT_PER_1M")
    if not p_in and not p_out:
        return None
    return round(tokens_in / 1e6 * p_in + tokens_out / 1e6 * p_out, 6)


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

    def __init__(self):
        self.key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

    @property
    def available(self) -> bool:
        return bool(self.key)

    async def complete(self, system: str, user: str, schema: dict) -> LLMReply:
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

    def __init__(self):
        self.key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

    @property
    def available(self) -> bool:
        return bool(self.key)

    async def complete(self, system: str, user: str, schema: dict) -> LLMReply:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user + "\n\nОтветь строго JSON по схеме:\n" + json.dumps(schema, ensure_ascii=False)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, json=body, headers={"x-goog-api-key": self.key})
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"Gemini: HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            raise LLMError(f"Gemini: {type(exc).__name__}") from None
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
        except (KeyError, IndexError, ValueError):
            raise LLMError("Gemini вернул ответ без JSON") from None
        usage = payload.get("usageMetadata", {})
        return LLMReply(data, self.model, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))


PROVIDERS = {"openai": OpenAIProvider, "gemini": GeminiProvider}


def provider_chain() -> list:
    """Основной провайдер + запасной (если для него задан ключ)."""
    primary = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    fallback = os.getenv("LLM_FALLBACK", "gemini").strip().lower()
    chain = []
    for name in (primary, fallback):
        cls = PROVIDERS.get(name)
        if cls and name not in [p.name for p in chain]:
            p = cls()
            if p.available:
                chain.append(p)
    return chain


def status() -> dict:
    chain = provider_chain()
    return {
        "configured": bool(chain),
        "providers": [{"name": p.name, "model": p.model} for p in chain],
    }
