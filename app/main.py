"""FastAPI: API + раздача интерфейса из папки web/."""
import asyncio
import html
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from . import llm  # noqa: E402
from .dataset import DatasetError, extract_records, fetch_from_organizer, save_records  # noqa: E402
from .db import connect, dumps, init_db, loads, now  # noqa: E402
from .default_checklist import DEFAULT_CHECKLIST  # noqa: E402
from .importers import CSV_TEMPLATE, parse_file  # noqa: E402
from . import media  # noqa: E402
from . import results as results_mod  # noqa: E402
from .evaluator import evaluate, semaphore  # noqa: E402
from .scoring import ChecklistError, allowed_answers, compute, validate_checklist  # noqa: E402

@asynccontextmanager
async def lifespan(_app):
    init_db(DEFAULT_CHECKLIST)
    llm.warm_up()
    yield


app = FastAPI(title="CQA — оценка качества диалогов", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
_tasks: set = set()


@app.middleware("http")
async def no_cache_ui(request, call_next):
    # Файлы интерфейса не кэшируем: после обновления всегда загружается свежая версия.
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------- вспомогательные ----------

def _conversation(conn, cid: str) -> dict:
    row = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Диалог не найден")
    return {**dict(row), "transcript": loads(row["transcript"], [])}


def _checklist(conn, checklist_id: int | None = None) -> dict:
    if checklist_id is None:
        row = conn.execute("SELECT * FROM checklists ORDER BY id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT * FROM checklists WHERE id=?", (checklist_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Чек-лист не найден")
    return {"id": row["id"], "version": row["version"], "comment": row["comment"], "created_at": row["created_at"], "data": loads(row["data"])}


def _final(evaluation: dict, checklist: dict, appeals: list) -> dict | None:
    """Финальный результат = исходные ответы модели + решения проверяющего по апелляциям."""
    results = evaluation.get("results")
    if not results:
        return None
    answers = {cid: r["answer"] for cid, r in results.items()}
    changed = False
    for a in appeals:
        if a["status"] == "decided" and a["decision"] == "change" and a["new_answer"]:
            answers[a["criterion_id"]] = a["new_answer"]
            changed = True
    calc = compute(checklist["data"], answers)
    return {"answers": answers, "score": calc["score"], "critical": calc["critical"], "sections": calc["sections"], "criteria": calc["criteria"], "changed": changed}


def _evaluation(conn, eid: int, with_final: bool = True) -> dict:
    row = conn.execute("SELECT * FROM evaluations WHERE id=?", (eid,)).fetchone()
    if not row:
        raise HTTPException(404, "Оценка не найдена")
    ev = dict(row)
    for key, default in (("results", None), ("sections", {}), ("issues", []), ("speech", None)):
        ev[key] = loads(ev[key], default)
    ev["critical"] = None if ev["critical"] is None else bool(ev["critical"])
    ev["appeals"] = [dict(a) for a in conn.execute("SELECT * FROM appeals WHERE evaluation_id=? ORDER BY id", (eid,))]
    if with_final and ev["results"]:
        ev["final"] = _final(ev, _checklist(conn, ev["checklist_id"]), ev["appeals"])
    return ev


async def _run(eid: int):
    limit = int(os.getenv("EVAL_CONCURRENCY", "3") or 3)
    async with semaphore(limit):
        with connect() as conn:
            row = conn.execute("SELECT conversation_id, checklist_id, requested_model FROM evaluations WHERE id=?", (eid,)).fetchone()
            conv = _conversation(conn, row["conversation_id"])
            checklist = _checklist(conn, row["checklist_id"])["data"]
            conn.execute("UPDATE evaluations SET status='running' WHERE id=?", (eid,))
        # Звонок с записью: скачиваем звук, если есть модель, которая умеет его слушать (Gemini)
        audio, audio_note = None, None
        if conv["channel"] == "voice call" and conv.get("audio_url") and llm.audio_provider(row["requested_model"]):
            try:
                audio = await media.download_audio(conv["id"], conv["audio_url"])
            except media.MediaError as exc:
                audio_note = f"Запись не проанализирована: {exc}. Звонок оценён по транскрипту."
        try:
            out = await evaluate(conv, checklist, row["requested_model"], audio=audio, audio_note=audio_note)
        except Exception as exc:  # ошибка API не превращается в нулевую оценку
            with connect() as conn:
                conn.execute("UPDATE evaluations SET status='failed', error=?, finished_at=? WHERE id=?", (str(exc)[:1000], now(), eid))
            return
        with connect() as conn:
            conn.execute(
                "UPDATE evaluations SET status=?, results=?, score=?, sections=?, critical=?, issues=?, model=?, "
                "tokens_in=?, tokens_out=?, cost_usd=?, speech=?, finished_at=? WHERE id=?",
                (out["status"], dumps(out["results"]), out["score"], dumps(out["sections"]), int(out["critical"]),
                 dumps(out["issues"]), out["model"], out["tokens_in"], out["tokens_out"], out["cost_usd"],
                 dumps(out["speech"]) if out.get("speech") else None, now(), eid),
            )


def _start(conversation_id: str, checklist_id: int, model: str | None = None) -> int:
    with connect() as conn:
        _conversation(conn, conversation_id)
        busy = conn.execute(
            "SELECT id FROM evaluations WHERE conversation_id=? AND status IN ('queued','running')", (conversation_id,)
        ).fetchone()
        if busy:
            return busy["id"]
        cur = conn.execute(
            "INSERT INTO evaluations(conversation_id, checklist_id, status, created_at, requested_model) VALUES (?,?, 'queued', ?, ?)",
            (conversation_id, checklist_id, now(), model),
        )
        eid = cur.lastrowid
    task = asyncio.get_running_loop().create_task(_run(eid))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return eid


# ---------- статус и данные ----------

@app.get("/api/status")
def status():
    with connect() as conn:
        counts = {
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "groups": [dict(r) for r in conn.execute("SELECT group_id, COUNT(*) AS total FROM conversations GROUP BY group_id")],
            "open_appeals": conn.execute("SELECT COUNT(*) FROM appeals WHERE status='open'").fetchone()[0],
        }
    return {"llm": llm.status(), "organizer_configured": bool(os.getenv("ORGANIZER_API_URL", "").strip()), **counts}


class FetchIn(BaseModel):
    group_id: str = "DEV"


@app.post("/api/dataset/fetch")
async def dataset_fetch(body: FetchIn):
    try:
        return await fetch_from_organizer(body.group_id)
    except DatasetError as exc:
        raise HTTPException(400, str(exc)) from None


class ImportIn(BaseModel):
    group_id: str = "MANUAL"
    payload: dict | list


@app.post("/api/dataset/import")
def dataset_import(body: ImportIn):
    try:
        return save_records(extract_records(body.payload), body.group_id.strip() or "MANUAL")
    except DatasetError as exc:
        raise HTTPException(400, str(exc)) from None


MAX_UPLOAD = 20 * 1024 * 1024


@app.post("/api/dataset/upload")
async def dataset_upload(request: Request, filename: str, group_id: str = "MANUAL", language: str = "AUTO", channel: str = "agent chat"):
    """Тело запроса — содержимое файла. Формат определяется по расширению имени файла."""
    data = await request.body()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Файл больше 20 МБ")
    if channel not in ("agent chat", "voice call"):
        raise HTTPException(400, "Канал: agent chat или voice call")
    try:
        records = parse_file(filename, data, language, channel)
        result = save_records(records, group_id.strip() or "MANUAL")
    except DatasetError as exc:
        raise HTTPException(400, f"{filename}: {exc}") from None
    return {**result, "filename": filename, "ids": [r["conversation_id"] for r in records][:50]}


@app.delete("/api/datasets/{group_id}")
def dataset_delete(group_id: str):
    """Удаляет набор целиком: диалоги, их оценки и апелляции. Чек-листы не трогает."""
    with connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM conversations WHERE group_id=?", (group_id,))]
        if not ids:
            raise HTTPException(404, f"Набора «{group_id}» нет")
        marks = ",".join("?" * len(ids))
        busy = conn.execute(f"SELECT COUNT(*) FROM evaluations WHERE conversation_id IN ({marks}) AND status IN ('queued','running')", ids).fetchone()[0]
        if busy:
            raise HTTPException(409, "В наборе идут оценки — дождитесь их окончания")
        conn.execute(f"DELETE FROM appeals WHERE evaluation_id IN (SELECT id FROM evaluations WHERE conversation_id IN ({marks}))", ids)
        evals = conn.execute(f"DELETE FROM evaluations WHERE conversation_id IN ({marks})", ids).rowcount
        conn.execute(f"DELETE FROM conversations WHERE id IN ({marks})", ids)
    for cid in ids:
        media.forget(cid)
    return {"group_id": group_id, "conversations": len(ids), "evaluations": evals}


@app.get("/api/dataset/template.csv")
def dataset_template():
    return Response(CSV_TEMPLATE.encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="cqa-template.csv"'})


@app.get("/api/conversations")
def conversations():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM conversations ORDER BY group_id, id").fetchall()
        latest = {
            r["conversation_id"]: r["id"]
            for r in conn.execute("SELECT conversation_id, MAX(id) AS id FROM evaluations GROUP BY conversation_id")
        }
        out = []
        for r in rows:
            transcript = loads(r["transcript"], [])
            item = {
                "id": r["id"], "group_id": r["group_id"], "channel": r["channel"], "language": r["language"],
                "messages": len(transcript), "audio_url": r["audio_url"],
                "preview": next((m["text"][:120] for m in transcript if m["speaker"] == "client"), ""),
                "evaluation": None,
            }
            if r["id"] in latest:
                ev = _evaluation(conn, latest[r["id"]])
                final = ev.get("final") or {}
                item["evaluation"] = {
                    "id": ev["id"], "status": ev["status"], "score": final.get("score", ev["score"]),
                    "original_score": ev["score"], "critical": final.get("critical", ev["critical"]),
                    "original_critical": ev["critical"],
                    "open_appeals": sum(a["status"] == "open" for a in ev["appeals"]),
                    "checklist_id": ev["checklist_id"],
                }
            out.append(item)
    return out


@app.get("/api/conversations/{cid}")
def conversation(cid: str):
    with connect() as conn:
        conv = _conversation(conn, cid)
        ids = [r["id"] for r in conn.execute("SELECT id FROM evaluations WHERE conversation_id=? ORDER BY id DESC", (cid,))]
        conv["evaluations"] = [
            {k: v for k, v in _evaluation(conn, i, with_final=False).items() if k in ("id", "status", "score", "checklist_id", "created_at", "model")}
            for i in ids
        ]
        conv["latest_evaluation_id"] = ids[0] if ids else None
    return conv


@app.get("/api/conversations/{cid}/audio")
async def conversation_audio(cid: str):
    """Звонки: перенаправляет браузер на временную ссылку mp3 со страницы плеера организаторов."""
    with connect() as conn:
        page_url = _conversation(conn, cid)["audio_url"]
    try:
        return RedirectResponse(await media.resolve_audio(cid, page_url))
    except media.MediaError as exc:
        raise HTTPException(exc.status, str(exc)) from None


# ---------- чек-лист ----------

@app.get("/api/checklists")
def checklists():
    with connect() as conn:
        return [
            {"id": r["id"], "version": r["version"], "comment": r["comment"], "created_at": r["created_at"],
             "criteria": len(loads(r["data"])["criteria"])}
            for r in conn.execute("SELECT * FROM checklists ORDER BY id DESC")
        ]


@app.get("/api/checklists/latest")
def checklist_latest():
    with connect() as conn:
        return _checklist(conn)


@app.get("/api/checklists/{checklist_id}")
def checklist_get(checklist_id: int):
    with connect() as conn:
        return _checklist(conn, checklist_id)


class ChecklistIn(BaseModel):
    data: dict
    comment: str = ""


@app.post("/api/checklists")
def checklist_save(body: ChecklistIn):
    try:
        data = validate_checklist(body.data)
    except ChecklistError as exc:
        raise HTTPException(400, str(exc)) from None
    with connect() as conn:
        version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM checklists").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO checklists(version, data, comment, created_at) VALUES (?,?,?,?)",
            (version, dumps(data), body.comment.strip() or f"Версия {version}", now()),
        )
        return _checklist(conn, cur.lastrowid)


# ---------- оценки ----------

class EvaluateIn(BaseModel):
    conversation_id: str
    checklist_id: int | None = None
    model: str | None = None  # «провайдер:модель» из /api/status → llm.models


def _check_model(model: str | None) -> str | None:
    if not model:
        return None
    if model not in {m["id"] for m in llm.available_models()}:
        raise HTTPException(400, f"Модель «{model}» недоступна. Список моделей задаётся в .env (OPENAI_MODELS).")
    return model


@app.post("/api/evaluations", status_code=202)
async def evaluation_start(body: EvaluateIn):
    if not llm.status()["configured"]:
        raise HTTPException(503, "Модель не настроена: укажите OPENAI_API_KEY в .env и перезапустите сервер.")
    with connect() as conn:
        checklist_id = _checklist(conn, body.checklist_id)["id"]
    return {"id": _start(body.conversation_id, checklist_id, _check_model(body.model))}


class BatchIn(BaseModel):
    conversation_ids: list[str] = Field(default_factory=list)
    group_id: str | None = None
    only_unevaluated: bool = True
    checklist_id: int | None = None
    model: str | None = None


@app.post("/api/evaluations/batch", status_code=202)
async def evaluation_batch(body: BatchIn):
    if not llm.status()["configured"]:
        raise HTTPException(503, "Модель не настроена: укажите OPENAI_API_KEY в .env и перезапустите сервер.")
    with connect() as conn:
        checklist_id = _checklist(conn, body.checklist_id)["id"]
        if body.conversation_ids:
            ids = body.conversation_ids
        elif body.group_id:
            ids = [r["id"] for r in conn.execute("SELECT id FROM conversations WHERE group_id=? ORDER BY id", (body.group_id,))]
        else:
            ids = [r["id"] for r in conn.execute("SELECT id FROM conversations ORDER BY id")]
        if body.only_unevaluated:
            done = {
                r["conversation_id"]
                for r in conn.execute("SELECT DISTINCT conversation_id FROM evaluations WHERE checklist_id=? AND status IN ('done','needs_review','queued','running')", (checklist_id,))
            }
            ids = [i for i in ids if i not in done]
    model = _check_model(body.model)
    return {"started": [_start(i, checklist_id, model) for i in ids], "checklist_id": checklist_id}


@app.get("/api/evaluations/{eid}")
def evaluation_get(eid: int):
    with connect() as conn:
        ev = _evaluation(conn, eid)
        ev["checklist"] = _checklist(conn, ev["checklist_id"])
    return ev


# ---------- апелляции ----------

class AppealIn(BaseModel):
    evaluation_id: int
    criterion_id: str
    comment: str


@app.post("/api/appeals", status_code=201)
def appeal_create(body: AppealIn):
    if not body.comment.strip():
        raise HTTPException(400, "Добавьте комментарий к апелляции")
    with connect() as conn:
        ev = _evaluation(conn, body.evaluation_id, with_final=False)
        if not ev["results"] or body.criterion_id not in ev["results"]:
            raise HTTPException(400, "По этому критерию нет оценки")
        if conn.execute("SELECT 1 FROM appeals WHERE evaluation_id=? AND criterion_id=?", (body.evaluation_id, body.criterion_id)).fetchone():
            raise HTTPException(409, "По этому критерию апелляция уже подана")
        cur = conn.execute(
            "INSERT INTO appeals(evaluation_id, criterion_id, comment, status, created_at) VALUES (?,?,?, 'open', ?)",
            (body.evaluation_id, body.criterion_id, body.comment.strip(), now()),
        )
        return dict(conn.execute("SELECT * FROM appeals WHERE id=?", (cur.lastrowid,)).fetchone())


@app.get("/api/appeals")
def appeals(status: str | None = None):
    with connect() as conn:
        sql = "SELECT a.*, e.conversation_id FROM appeals a JOIN evaluations e ON e.id=a.evaluation_id"
        rows = conn.execute(sql + (" WHERE a.status=?" if status else "") + " ORDER BY a.status='decided', a.id DESC", (status,) if status else ()).fetchall()
        return [dict(r) for r in rows]


class DecisionIn(BaseModel):
    decision: str  # confirm | change
    new_answer: str | None = None
    reason: str = ""


def _decision_answers(conn, appeal_id: int, body: DecisionIn):
    appeal = conn.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
    if not appeal:
        raise HTTPException(404, "Апелляция не найдена")
    ev = _evaluation(conn, appeal["evaluation_id"], with_final=False)
    checklist = _checklist(conn, ev["checklist_id"])
    criterion = next(c for c in checklist["data"]["criteria"] if c["id"] == appeal["criterion_id"])
    if body.decision not in ("confirm", "change"):
        raise HTTPException(400, "Решение: confirm или change")
    new_answer = None
    if body.decision == "change":
        new_answer = (body.new_answer or "").strip().lower()
        if new_answer not in allowed_answers(criterion):
            raise HTTPException(400, f"Допустимые ответы: {', '.join(allowed_answers(criterion))}")
        if new_answer == ev["results"][appeal["criterion_id"]]["answer"]:
            raise HTTPException(400, "Новая оценка совпадает с исходной — выберите «Подтвердить»")
    return appeal, ev, checklist, new_answer


@app.post("/api/appeals/{appeal_id}/preview")
def appeal_preview(appeal_id: int, body: DecisionIn):
    with connect() as conn:
        appeal, ev, checklist, new_answer = _decision_answers(conn, appeal_id, body)
        others = [a for a in ev["appeals"] if a["id"] != appeal_id]
        draft = {**dict(appeal), "status": "decided", "decision": body.decision, "new_answer": new_answer}
        return _final(ev, checklist, others + [draft])


@app.post("/api/appeals/{appeal_id}/decide")
def appeal_decide(appeal_id: int, body: DecisionIn):
    if not body.reason.strip():
        raise HTTPException(400, "Укажите причину решения")
    with connect() as conn:
        appeal, ev, checklist, new_answer = _decision_answers(conn, appeal_id, body)
        if appeal["status"] == "decided":
            raise HTTPException(409, "Решение по апелляции уже принято")
        conn.execute(
            "UPDATE appeals SET status='decided', decision=?, new_answer=?, reason=?, decided_at=? WHERE id=?",
            (body.decision, new_answer, body.reason.strip(), now(), appeal_id),
        )
        return _evaluation(conn, ev["id"])


# ---------- аналитика ----------

def _avg(values):
    return round(sum(values) / len(values), 2) if values else None


@app.get("/api/analytics")
def analytics(group_id: str | None = None):
    """Сводка по последней оценке каждого диалога, с учётом решений по апелляциям."""
    with connect() as conn:
        convs = [dict(r) for r in conn.execute("SELECT id, group_id, channel, language FROM conversations ORDER BY id")]
        if group_id:
            convs = [c for c in convs if c["group_id"] == group_id]
        latest = {
            r["conversation_id"]: r["id"]
            for r in conn.execute("SELECT conversation_id, MAX(id) AS id FROM evaluations GROUP BY conversation_id")
        }
        checklists: dict = {}
        statuses = {"none": 0, "queued": 0, "running": 0, "done": 0, "needs_review": 0, "failed": 0}
        scored, by_lang, by_channel, criteria, critical, rows = [], {}, {}, {}, [], []
        cost = tokens_in = tokens_out = 0.0
        evaluations_total = 0
        for c in convs:
            eid = latest.get(c["id"])
            if not eid:
                statuses["none"] += 1
                continue
            ev = _evaluation(conn, eid)
            statuses[ev["status"]] = statuses.get(ev["status"], 0) + 1
            if not ev["results"]:
                continue
            evaluations_total += 1
            cost += ev["cost_usd"] or 0
            tokens_in += ev["tokens_in"] or 0
            tokens_out += ev["tokens_out"] or 0
            if ev["checklist_id"] not in checklists:
                checklists[ev["checklist_id"]] = _checklist(conn, ev["checklist_id"])["data"]
            cl = checklists[ev["checklist_id"]]
            final = ev["final"]
            score = float(final["score"]) if final["score"] is not None else None
            if score is not None:
                scored.append(score)
                by_lang.setdefault(c["language"], []).append(score)
                by_channel.setdefault(c["channel"], []).append(score)
            if final["critical"]:
                crit_c = next((x for x in cl["criteria"] if x["type"] == "flag" and final["answers"].get(x["id"]) == "yes"), None)
                critical.append({
                    "conversation_id": c["id"], "language": c["language"], "channel": c["channel"], "score": final["score"],
                    "reason": ev["results"].get(crit_c["id"], {}).get("explanation", "") if crit_c else "",
                    "status": ev["status"],
                })
            rows.append({"conversation_id": c["id"], "language": c["language"], "channel": c["channel"], "score": final["score"], "critical": final["critical"], "status": ev["status"]})
            for crit in cl["criteria"]:
                answer = final["answers"].get(crit["id"])
                if answer is None:
                    continue
                s = criteria.setdefault(crit["id"], {"id": crit["id"], "title": crit["title"], "type": crit["type"], "weight": crit["weight"], "evaluated": 0, "na": 0, "fulfilment": [], "violations": 0})
                s["evaluated"] += 1
                if answer == "na":
                    s["na"] += 1
                elif crit["type"] == "flag":
                    s["violations"] += answer == "yes"
                else:
                    item = final["criteria"][crit["id"]]
                    s["fulfilment"].append(float(item["points"]) / float(item["weight"]) * 100)
        appeals = [dict(a) for a in conn.execute("SELECT a.status, a.decision, e.conversation_id FROM appeals a JOIN evaluations e ON e.id=a.evaluation_id")]
        all_groups = [r[0] for r in conn.execute("SELECT DISTINCT group_id FROM conversations ORDER BY 1")]
    if group_id:
        ids = {c["id"] for c in convs}
        appeals = [a for a in appeals if a["conversation_id"] in ids]
    buckets = [("0–19", 0, 20), ("20–39", 20, 40), ("40–59", 40, 60), ("60–79", 60, 80), ("80–99", 80, 100), ("100", 100, 101)]
    crit_out = []
    for s in criteria.values():
        crit_out.append({
            "id": s["id"], "title": s["title"], "type": s["type"], "weight": s["weight"], "evaluated": s["evaluated"], "na": s["na"],
            "fulfilment": _avg(s["fulfilment"]), "violations": s["violations"],
        })
    scored_sorted = sorted(scored)
    median = None
    if scored_sorted:
        mid = len(scored_sorted) // 2
        median = scored_sorted[mid] if len(scored_sorted) % 2 else round((scored_sorted[mid - 1] + scored_sorted[mid]) / 2, 2)
    return {
        "group_id": group_id,
        "groups": all_groups,
        "total": len(convs),
        "statuses": statuses,
        "avg_score": _avg(scored),
        "median_score": median,
        "distribution": [{"label": lbl, "count": sum(lo <= s < hi for s in scored)} for lbl, lo, hi in buckets],
        "by_language": [{"key": k, "count": len(v), "avg": _avg(v)} for k, v in sorted(by_lang.items())],
        "by_channel": [{"key": k, "count": len(v), "avg": _avg(v)} for k, v in sorted(by_channel.items())],
        "criteria": crit_out,
        "critical": critical,
        "lowest": sorted([r for r in rows if r["score"] is not None], key=lambda r: float(r["score"]))[:10],
        "appeals": {
            "total": len(appeals),
            "open": sum(a["status"] == "open" for a in appeals),
            "confirmed": sum(a["decision"] == "confirm" for a in appeals),
            "changed": sum(a["decision"] == "change" for a in appeals),
        },
        "cost": {
            "total_usd": round(cost, 4), "per_evaluation_usd": round(cost / evaluations_total, 5) if evaluations_total else None,
            "tokens_in": int(tokens_in), "tokens_out": int(tokens_out), "evaluations": evaluations_total,
        },
    }


# ---------- сдача результатов (формат организаторов JSON 1.0) ----------

def _results_payload(group_id: str, use_appeals: bool) -> tuple[dict, list[str]]:
    with connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM conversations WHERE group_id=? ORDER BY id", (group_id,))]
        if not ids:
            raise HTTPException(404, f"В наборе «{group_id}» нет диалогов")
        records, models = [], []
        for cid in ids:
            row = conn.execute(
                "SELECT id FROM evaluations WHERE conversation_id=? AND status IN ('done','needs_review') ORDER BY id DESC LIMIT 1", (cid,)
            ).fetchone() or conn.execute("SELECT id FROM evaluations WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            ev = _evaluation(conn, row["id"]) if row else None
            checklist = _checklist(conn, ev["checklist_id"])["data"] if ev else None
            if ev and ev.get("model"):
                models.append(ev["model"].split(" (")[0])
            records.append(results_mod.build_record(cid, ev, checklist, use_appeals))
    payload = {
        "schema_version": "1.0",
        "team_name": results_mod.team_name(),
        "model": results_mod.model_block(models),
        "results": records,
    }
    return payload, results_mod.validate(payload, ids)


@app.get("/api/results")
def results_get(group_id: str, use_appeals: bool = False):
    payload, problems = _results_payload(group_id, use_appeals)
    return {"payload": payload, "problems": problems, "total": len(payload["results"]),
            "ok": sum(r["status"] == "ok" for r in payload["results"])}


class ResultsSaveIn(BaseModel):
    group_id: str
    target: str  # dev | prod
    use_appeals: bool = False


@app.post("/api/results/save")
def results_save(body: ResultsSaveIn):
    payload, problems = _results_payload(body.group_id, body.use_appeals)
    try:
        root = Path(os.getenv("RESULTS_ROOT", "") or ROOT)
        path = results_mod.write(payload, body.target, root)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"path": str(path.relative_to(root)).replace("\\", "/"), "problems": problems, "total": len(payload["results"]),
            "ok": sum(r["status"] == "ok" for r in payload["results"])}


# ---------- экспорт ----------

@app.get("/api/export")
def export(group_id: str | None = None):
    rows = conversations()
    out = []
    with connect() as conn:
        for r in rows:
            if group_id and r["group_id"] != group_id:
                continue
            item = {"conversation_id": r["id"], "group_id": r["group_id"], "channel": r["channel"], "language": r["language"], "evaluation": None}
            if r["evaluation"]:
                ev = _evaluation(conn, r["evaluation"]["id"])
                checklist = _checklist(conn, ev["checklist_id"])
                item["evaluation"] = {
                    "status": ev["status"], "checklist_version": checklist["version"], "model": ev["model"],
                    "original_score": ev["score"], "final_score": (ev.get("final") or {}).get("score"),
                    "critical": (ev.get("final") or {}).get("critical"), "criteria": ev["results"], "appeals": ev["appeals"],
                    "cost_usd": ev["cost_usd"],
                }
            out.append(item)
    return JSONResponse(out, headers={"Content-Disposition": f'attachment; filename="cqa-export-{group_id or "all"}.json"'})


# ---------- интерфейс ----------

WEB = ROOT / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
