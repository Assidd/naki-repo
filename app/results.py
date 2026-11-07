"""Выгрузка результатов в формате сдачи организаторам (Приложение к гайду, формат JSON 1.0).

results/dev/results.json  — DEV (60 чатов и 5 звонков)
results/prod/results.json — контрольный набор
"""
import json
import os
import re
from collections import Counter
from pathlib import Path

# Коды критериев организаторов. Наши ID c1…c8 из начального чек-листа сопоставляются с ними;
# критерий с ID, равным коду организаторов, выгружается как есть.
OFFICIAL_CODES = ["contact", "discovery", "identification", "consultation", "empathy", "dialogue", "closure", "critical"]
ID_TO_CODE = dict(zip(["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"], OFFICIAL_CODES))


def criterion_code(criterion_id: str) -> str:
    return ID_TO_CODE.get(criterion_id, criterion_id)


def _value(criterion: dict, answer: str):
    if answer == "na":
        return None
    if criterion["type"] in ("yes_no", "flag"):
        return answer == "yes"
    number = float(answer)
    return int(number) if number.is_integer() else number


def _turns(result: dict) -> list[int]:
    nums = [int(e["message_id"][1:]) for e in result.get("evidence", []) if re.fullmatch(r"m\d+", e.get("message_id", ""))]
    if not nums and result.get("checked_range"):
        ids = [int(x) for x in re.findall(r"m(\d+)", result["checked_range"])]
        if ids:
            nums = list(range(min(ids), max(ids) + 1))
    return sorted(set(nums))


def build_record(conversation_id: str, evaluation: dict | None, checklist: dict | None, use_appeals: bool) -> dict:
    if not evaluation or not evaluation.get("results"):
        reason = (evaluation or {}).get("error") or ("Оценка ещё не выполнена" if not evaluation else "Модель не вернула результат")
        return {"conversation_id": conversation_id, "status": "error", "criteria": [], "score": None, "error": reason}
    answers = {cid: r["answer"] for cid, r in evaluation["results"].items()}
    score, critical = evaluation["score"], evaluation["critical"]
    if use_appeals and evaluation.get("final"):
        answers = evaluation["final"]["answers"]
        score, critical = evaluation["final"]["score"], evaluation["final"]["critical"]
    criteria = []
    for c in checklist["criteria"]:
        r = evaluation["results"].get(c["id"])
        if r is None:
            continue
        criteria.append({
            "criterion_id": criterion_code(c["id"]),
            "value": _value(c, answers[c["id"]]),
            "rationale": r.get("explanation", ""),
            "evidence_turns": _turns(r),
        })
    return {
        "conversation_id": conversation_id,
        "status": "ok",
        "criteria": criteria,
        "score": {"final_score": None if score is None else float(score), "critical_flag": bool(critical)},
    }


def validate(payload: dict, expected_ids: list[str]) -> list[str]:
    """Проверки из раздела «Перед push»: все ID, без повторов, у ok — ровно 8 официальных критериев."""
    problems = []
    ids = [r["conversation_id"] for r in payload["results"]]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    if dup:
        problems.append("Повторяются ID: " + ", ".join(dup))
    missing = sorted(set(expected_ids) - set(ids))
    if missing:
        problems.append("Нет записей для: " + ", ".join(missing[:10]))
    for r in payload["results"]:
        if r["status"] == "error":
            problems.append(f"{r['conversation_id']}: status=error — {r.get('error')}")
            continue
        codes = [c["criterion_id"] for c in r["criteria"]]
        if sorted(codes) != sorted(OFFICIAL_CODES):
            extra = sorted(set(codes) - set(OFFICIAL_CODES))
            lacking = sorted(set(OFFICIAL_CODES) - set(codes))
            problems.append(f"{r['conversation_id']}: критерии не совпадают с кодами организаторов"
                            + (f"; лишние: {', '.join(extra)}" if extra else "") + (f"; нет: {', '.join(lacking)}" if lacking else ""))
        if r["score"]["final_score"] is not None and not 0 <= r["score"]["final_score"] <= 100:
            problems.append(f"{r['conversation_id']}: итог вне диапазона 0–100")
    return problems


def model_block(model_names: list[str]) -> dict:
    names = sorted({m for m in model_names if m})
    local = [n for n in names if n.startswith("local:")]
    mode = "local" if names and len(local) == len(names) else "hybrid" if local else "external_api"
    clean = sorted({re.sub(r"^[a-z]+:", "", n) for n in names})
    return {"mode": mode, "name": ", ".join(clean) or "не использовалась"}


def write(payload: dict, target: str, root: Path) -> Path:
    if target not in ("dev", "prod"):
        raise ValueError("target: dev или prod")
    path = root / "results" / target / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def team_name() -> str:
    return os.getenv("TEAM_NAME", "").strip() or "naki"
