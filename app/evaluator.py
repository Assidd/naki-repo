"""Оценка диалога моделью: промпт → ответ → проверка доказательств → расчёт.

Модель только выбирает ответы и приводит цитаты. Баллы, перераспределение N/A и итог
считает сервер (scoring.py). Цитата засчитывается, только если она дословно есть
в указанной реплике.
"""
import asyncio
import re
from decimal import Decimal, InvalidOperation

from . import llm
from .scoring import allowed_answers, compute

PROMPT_VERSION = "v2"

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criteria"],
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["criterion_id", "answer", "explanation", "evidence", "checked_range"],
                "properties": {
                    "criterion_id": {"type": "string"},
                    "answer": {"type": "string"},
                    "explanation": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["message_id", "quote"],
                            "properties": {"message_id": {"type": "string"}, "quote": {"type": "string"}},
                        },
                    },
                    "checked_range": {"type": ["string", "null"]},
                },
            },
        }
    },
}

SYSTEM_PROMPT = """Ты — специалист контроля качества клиентской поддержки брокера Freedom Broker.
Оцени работу ОПЕРАТОРА в диалоге строго по критериям чек-листа.

Правила:
1. Оценивай только реплики operator. Реплики client, bot и system — контекст (например, сообщение system о закрытии чата).
2. Применяй критерий ровно в той формулировке, что дана. Не добавляй условий, которых в тексте критерия нет.
   Если для «Да» в описании перечислено несколько действий через «и» (например, «поздоровался и представился»), «Да» ставь только когда выполнены ВСЕ; если хотя бы одного нет — «Нет», и в объяснении назови, чего не хватило.
   Сначала проверь условие N/A: если оно выполнено (например, данных для ответа хватало с самого начала и уточнять было нечего), ставь "na", а не "yes".
   Цитата должна подтверждать именно это условие: реплика, которая относится к другому критерию (например, запрос данных для проверки личности — к идентификации, а не к выявлению потребности), не подходит.
3. Ответ (answer):
   - для yes_no: "yes" или "no";
   - для scale: одно из чисел, перечисленных в шкале критерия (например "15");
   - для flag (грубые ошибки): "yes" — нарушение найдено, "no" — не найдено;
   - "na" — только если N/A разрешён для критерия и выполнено его условие неприменимости.
   "no" значит: действие было нужно, но оператор его пропустил или выполнил неправильно. Это не то же самое, что N/A.
4. Доказательства (evidence): message_id реплики (например "m3") и quote — короткий фрагмент, СКОПИРОВАННЫЙ ДОСЛОВНО из текста этой реплики, без перевода и пересказа.
   - Для "yes" и для шкальной оценки — минимум одна цитата, подтверждающая вывод.
   - Если действие пропущено ("no"), в checked_range укажи проверенный участок, где действие ожидалось (например "m1-m4"); цитаты — если есть подходящие.
   - Для flag="yes" обязательно процитируй реплику с нарушением.
   - Для "na" объясни, какое условие неприменимости выполнено; evidence может быть пустым.
   - Если checked_range не нужен — null.
5. explanation — 1–2 коротких предложения на русском языке: что сделал или не сделал оператор и почему такой ответ.
6. Не выдумывай факты о продуктах, тарифах и сроках компании. Если правильность консультации по существу проверить нельзя, оцени полноту и логичность ответа и прямо скажи в объяснении, что факт не проверен.
7. В звонках (voice call) транскрипт получен автоматическим распознаванием речи: ошибки распознавания и особенности устной речи — не ошибка оператора.
8. Верни ответ по каждому критерию из списка, ровно один раз."""


def _num(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def normalize_answer(criterion: dict, raw) -> str | None:
    text = str(raw if raw is not None else "").strip().lower().strip('"')
    aliases = {"да": "yes", "нет": "no", "n/a": "na", "н/д": "na", "true": "yes", "false": "no"}
    text = aliases.get(text, text)
    allowed = allowed_answers(criterion)
    if text in allowed:
        return text
    number = _num(text)
    if number is not None and criterion["type"] == "scale":
        for a in allowed:
            if a != "na" and _num(a) == number:
                return a
    return None


def _norm_text(s: str) -> str:
    s = s.replace(" ", " ").replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"').replace("’", "'")
    return re.sub(r"\s+", " ", s).strip().casefold()


def quote_in_message(quote: str, text: str) -> bool:
    q = _norm_text(quote).strip(" .…\"'")
    if len(q) < 2:
        return False
    t = _norm_text(text)
    if q in t:
        return True
    # Модель иногда сокращает цитату многоточием — проверяем каждую часть.
    parts = [p.strip(" .\"'") for p in re.split(r"\.\.\.|…", q) if p.strip(" .\"'")]
    return len(parts) > 1 and all(p in t for p in parts)


def _range_ok(rng: str | None, ids: set) -> bool:
    if not rng:
        return False
    found = re.findall(r"m\d+", rng)
    return bool(found) and all(f in ids for f in found)


def build_user_prompt(conv: dict, checklist: dict) -> str:
    lines = []
    for c in checklist["criteria"]:
        head = f"[{c['id']}] {c['title']} — тип {c['type']}"
        if c["type"] == "scale":
            head += "; шкала: " + "; ".join(f"{s['value']} — {s['label']}" for s in c["scale"])
        head += f"\n  Описание: {c['description']}"
        if c.get("na_allowed"):
            head += f"\n  N/A разрешён, если: {c['na_condition']}"
        else:
            head += "\n  N/A не разрешён."
        lines.append(head)
    transcript = "\n".join(
        f"{m['id']} [{m['timestamp']}] {m['speaker']}: {m['text']}" for m in conv["transcript"]
    )
    notes = checklist.get("notes") or ""
    return (
        f"Канал: {conv['channel']}. Язык диалога: {conv['language']}.\n\n"
        f"ЧЕК-ЛИСТ:\n" + "\n".join(lines) + (f"\nПримечание: {notes}" if notes else "") +
        f"\n\nДИАЛОГ:\n{transcript}"
    )


def check_reply(data: dict, conv: dict, checklist: dict) -> tuple[dict, list[str], list[str]]:
    """Возвращает (results по критериям, структурные ошибки, проблемы доказательств)."""
    messages = {m["id"]: m for m in conv["transcript"]}
    ids = set(messages)
    items = data.get("criteria") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}, ["Ответ не содержит массива criteria"], []
    by_id = {}
    for item in items:
        if isinstance(item, dict) and item.get("criterion_id") not in by_id:
            by_id[item.get("criterion_id")] = item
    results, structural, evidence_issues = {}, [], []
    for c in checklist["criteria"]:
        item = by_id.get(c["id"])
        if item is None:
            structural.append(f"{c['id']}: нет ответа по критерию")
            continue
        answer = normalize_answer(c, item.get("answer"))
        if answer is None:
            structural.append(f"{c['id']}: недопустимый ответ «{item.get('answer')}», разрешено: {', '.join(allowed_answers(c))}")
            continue
        evidence = []
        for ev in item.get("evidence") or []:
            mid = str(ev.get("message_id") or "").strip()
            quote = str(ev.get("quote") or "").strip()
            msg = messages.get(mid)
            verified = bool(msg) and quote_in_message(quote, msg["text"])
            evidence.append({"message_id": mid, "quote": quote, "verified": verified})
            if not msg:
                evidence_issues.append(f"{c['id']}: реплики {mid} нет в диалоге")
            elif not verified:
                evidence_issues.append(f"{c['id']}: цитата не найдена в {mid}: «{quote[:80]}»")
        rng = item.get("checked_range")
        rng = str(rng).strip() if rng else None
        has_verified = any(e["verified"] for e in evidence)
        needs_quote = answer not in ("na", "no") and not (c["type"] == "scale" and _num(answer) == 0) and not (c["type"] == "flag" and answer == "no")
        if needs_quote and not has_verified:
            evidence_issues.append(f"{c['id']}: ответ «{answer}» не подтверждён проверенной цитатой")
        if answer == "no" and c["type"] != "flag" and not has_verified and not _range_ok(rng, ids):
            evidence_issues.append(f"{c['id']}: для «Нет» нужен проверенный участок (checked_range) или цитата")
        results[c["id"]] = {
            "criterion_id": c["id"],
            "answer": answer,
            "explanation": str(item.get("explanation") or "").strip(),
            "evidence": evidence,
            "checked_range": rng if _range_ok(rng, ids) else None,
        }
    return results, structural, evidence_issues


async def evaluate(conv: dict, checklist: dict) -> dict:
    """Возвращает словарь для сохранения в evaluations. Бросает llm.LLMError, если ни один провайдер не ответил."""
    chain = llm.provider_chain()
    if not chain:
        raise llm.LLMError("Не настроен ни один провайдер модели. Укажите OPENAI_API_KEY в .env и перезапустите сервер.")
    user_prompt = build_user_prompt(conv, checklist)
    errors = []
    for provider in chain:
        tokens_in = tokens_out = 0
        prompt = user_prompt
        try:
            best = None
            for attempt in range(2):  # первая попытка + одна попытка исправления
                reply = await provider.complete(SYSTEM_PROMPT, prompt, RESPONSE_SCHEMA)
                tokens_in += reply.tokens_in
                tokens_out += reply.tokens_out
                results, structural, ev_issues = check_reply(reply.data, conv, checklist)
                if not structural:
                    best = (results, ev_issues, reply.model)
                    if not ev_issues:
                        break
                feedback = structural + ev_issues
                prompt = (
                    user_prompt
                    + "\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН ПРОВЕРКОЙ. Исправь ошибки и верни полный ответ заново:\n- "
                    + "\n- ".join(feedback[:20])
                )
            if best is None:
                errors.append(f"{provider.name}: ответ не прошёл проверку структуры")
                continue
        except llm.LLMError as exc:
            errors.append(str(exc))
            continue
        results, ev_issues, model = best
        calc = compute(checklist, {cid: r["answer"] for cid, r in results.items()})
        for cid, r in results.items():
            r.update(calc["criteria"][cid])
        return {
            "status": "needs_review" if ev_issues else "done",
            "results": results,
            "score": calc["score"],
            "sections": calc["sections"],
            "critical": calc["critical"],
            "issues": ev_issues,
            "model": f"{provider.name}:{model} (prompt {PROMPT_VERSION})",
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": llm.cost_usd(provider.name, tokens_in, tokens_out),
        }
    raise llm.LLMError("; ".join(errors) or "Модель не ответила")


_semaphore: asyncio.Semaphore | None = None


def semaphore(limit: int) -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(limit)
    return _semaphore
