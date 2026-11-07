"""Проверка чек-листа и расчёт итога по правилам из PDF.

Порядок расчёта:
1. N/A только там, где он разрешён.
2. «Да» = полный вес, «Нет» = 0; шкала — выбранная ступень.
3. Шкала пересчитывается пропорционально весу (верхняя ступень = полный вес).
4. Критерии с N/A исключаются, их вес пропорционально перераспределяется — максимум остаётся 100.
5. Итог округляется до двух знаков (ROUND_HALF_UP). Нет применимых критериев с весом — итог не считается.
   Грубые ошибки (тип flag) отмечаются отдельно и в баллы не входят.
"""
from decimal import ROUND_HALF_UP, Decimal

TYPES = {"yes_no", "scale", "flag"}


class ChecklistError(ValueError):
    pass


def validate_checklist(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ChecklistError("Чек-лист должен быть объектом")
    criteria = data.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ChecklistError("В чек-листе должен быть хотя бы один критерий")
    sections = data.get("sections") or []
    section_ids = {s.get("id") for s in sections}
    seen = set()
    weighted = 0
    for c in criteria:
        cid = str(c.get("id") or "").strip()
        title = str(c.get("title") or "").strip()
        if not cid or not title:
            raise ChecklistError("У каждого критерия нужны id и название")
        if cid in seen:
            raise ChecklistError(f"Повторяется id критерия «{cid}»")
        seen.add(cid)
        if c.get("type") not in TYPES:
            raise ChecklistError(f"«{title}»: тип должен быть yes_no, scale или flag")
        if c.get("section") and c["section"] not in section_ids:
            raise ChecklistError(f"«{title}»: неизвестный раздел «{c['section']}»")
        try:
            weight = Decimal(str(c.get("weight", 0)))
        except Exception:
            raise ChecklistError(f"«{title}»: вес должен быть числом") from None
        if weight < 0:
            raise ChecklistError(f"«{title}»: вес не может быть отрицательным")
        if c["type"] == "flag" and weight != 0:
            raise ChecklistError(f"«{title}»: у отметки грубой ошибки нет веса")
        if c["type"] != "flag" and weight == 0:
            raise ChecklistError(f"«{title}»: у оцениваемого критерия вес должен быть больше 0")
        if c["type"] == "scale":
            scale = c.get("scale") or []
            values = []
            for step in scale:
                try:
                    values.append(Decimal(str(step.get("value"))))
                except Exception:
                    raise ChecklistError(f"«{title}»: ступени шкалы должны быть числами") from None
            if len(values) < 2:
                raise ChecklistError(f"«{title}»: у шкалы должно быть минимум две ступени")
            if len(set(values)) != len(values):
                raise ChecklistError(f"«{title}»: ступени шкалы повторяются")
            if min(values) < 0 or max(values) <= 0:
                raise ChecklistError(f"«{title}»: верхняя ступень должна быть больше нуля, ступени — не меньше нуля")
        if c.get("na_allowed") and not str(c.get("na_condition") or "").strip():
            raise ChecklistError(f"«{title}»: при разрешённом N/A укажите условие неприменимости")
        weighted += c["type"] != "flag"
    if not weighted:
        raise ChecklistError("Нужен хотя бы один критерий с весом")
    return data


def allowed_answers(criterion: dict) -> list[str]:
    if criterion["type"] == "scale":
        answers = [_fmt(Decimal(str(s["value"]))) for s in criterion["scale"]]
    else:
        answers = ["yes", "no"]
    if criterion.get("na_allowed"):
        answers.append("na")
    return answers


def _fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def criterion_points(criterion: dict, answer: str) -> Decimal | None:
    """Баллы критерия до перераспределения N/A. None — критерий не участвует в итоге."""
    if criterion["type"] == "flag" or answer == "na":
        return None
    weight = Decimal(str(criterion["weight"]))
    if criterion["type"] == "yes_no":
        return weight if answer == "yes" else Decimal(0)
    top = max(Decimal(str(s["value"])) for s in criterion["scale"])
    return weight * Decimal(answer) / top


def _round(value: Decimal, digits: int) -> str:
    q = Decimal(1).scaleb(-digits)
    return str(value.quantize(q, rounding=ROUND_HALF_UP))


def compute(checklist: dict, answers: dict[str, str]) -> dict:
    """answers: {criterion_id: 'yes'|'no'|'na'|'<число>'} — по всем критериям чек-листа."""
    digits = int(checklist.get("rounding", 2))
    per = {}
    total_points = Decimal(0)
    total_weight = Decimal(0)
    critical = False
    by_section: dict[str, list[Decimal]] = {}
    for c in checklist["criteria"]:
        answer = answers[c["id"]]
        if c["type"] == "flag":
            flagged = answer == "yes"
            critical = critical or flagged
            per[c["id"]] = {"points": None, "weight": "0", "share": None, "flag": flagged}
            continue
        points = criterion_points(c, answer)
        weight = Decimal(str(c["weight"]))
        if points is not None:
            total_points += points
            total_weight += weight
            acc = by_section.setdefault(c.get("section") or "", [Decimal(0), Decimal(0)])
            acc[0] += points
            acc[1] += weight
        per[c["id"]] = {"points": None if points is None else points, "weight": weight, "flag": None}
    # Перераспределение: каждый применимый критерий весит weight/total_weight*100.
    for cid, item in per.items():
        if item["points"] is None or total_weight == 0:
            item["share"] = None
            item["points"] = None if item["points"] is None else str(item["points"])
            item["weight"] = str(item["weight"])
            continue
        factor = Decimal(100) / total_weight
        item["share"] = _round(item["points"] * factor, digits)
        item["max_share"] = _round(item["weight"] * factor, digits)
        item["points"] = _round(item["points"], digits)
        item["weight"] = str(item["weight"])
    score = None if total_weight == 0 else _round(total_points / total_weight * 100, digits)
    sections = {
        sid: _round(p / w * 100, digits) for sid, (p, w) in by_section.items() if w > 0
    }
    return {"score": score, "critical": critical, "criteria": per, "sections": sections}
