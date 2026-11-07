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


# Ориентировочные цены USD за 1 млн токенов (вход, выход). Сверяйте с прайсом провайдера;
# для модели по умолчанию цену можно переопределить в .env (*_PRICE_INPUT_PER_1M / *_PRICE_OUTPUT_PER_1M).
KNOWN_PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
}

DEFAULT_OPENAI_MODELS = "gpt-4o-mini,gpt-4.1-mini,gpt-4.1,gpt-4o"


def cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int) -> float | None:
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

    def __init__(self, model: str | None = None):
        self.key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

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

    def __init__(self, model: str | None = None):
        self.key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

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


def available_models() -> list[dict]:
    """Модели, которые можно выбрать в интерфейсе: только у провайдеров с ключом.
    Список задаётся в .env (OPENAI_MODELS / GEMINI_MODELS через запятую)."""
    out = []
    lists = {
        "openai": os.getenv("OPENAI_MODELS", "").strip() or DEFAULT_OPENAI_MODELS,
        "gemini": os.getenv("GEMINI_MODELS", "").strip() or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
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


def status() -> dict:
    chain = provider_chain()
    return {
        "configured": bool(chain),
        "providers": [{"name": p.name, "model": p.model} for p in chain],
        "models": available_models(),
    }
