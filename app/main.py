"""FastAPI: API + раздача интерфейса из папки web/."""
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from . import llm  # noqa: E402
from .dataset import DatasetError, extract_records, fetch_from_organizer, save_records  # noqa: E402
from .db import connect, dumps, init_db, loads, now  # noqa: E402
from .default_checklist import DEFAULT_CHECKLIST  # noqa: E402
from .evaluator import evaluate, semaphore  # noqa: E402
from .scoring import ChecklistError, allowed_answers, compute, validate_checklist  # noqa: E402

@asynccontextmanager
async def lifespan(_app):
    init_db(DEFAULT_CHECKLIST)
    llm.warm_up()
    yield


app = FastAPI(title="CQA — оценка качества диалогов", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
_tasks: set = set()


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
    for key, default in (("results", None), ("sections", {}), ("issues", [])):
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
            row = conn.execute("SELECT conversation_id, checklist_id FROM evaluations WHERE id=?", (eid,)).fetchone()
            conv = _conversation(conn, row["conversation_id"])
            checklist = _checklist(conn, row["checklist_id"])["data"]
            conn.execute("UPDATE evaluations SET status='running' WHERE id=?", (eid,))
        try:
            out = await evaluate(conv, checklist)
        except Exception as exc:  # ошибка API не превращается в нулевую оценку
            with connect() as conn:
                conn.execute("UPDATE evaluations SET status='failed', error=?, finished_at=? WHERE id=?", (str(exc)[:1000], now(), eid))
            return
        with connect() as conn:
            conn.execute(
                "UPDATE evaluations SET status=?, results=?, score=?, sections=?, critical=?, issues=?, model=?, "
                "tokens_in=?, tokens_out=?, cost_usd=?, finished_at=? WHERE id=?",
                (out["status"], dumps(out["results"]), out["score"], dumps(out["sections"]), int(out["critical"]),
                 dumps(out["issues"]), out["model"], out["tokens_in"], out["tokens_out"], out["cost_usd"], now(), eid),
            )


def _start(conversation_id: str, checklist_id: int) -> int:
    with connect() as conn:
        _conversation(conn, conversation_id)
        busy = conn.execute(
            "SELECT id FROM evaluations WHERE conversation_id=? AND status IN ('queued','running')", (conversation_id,)
        ).fetchone()
        if busy:
            return busy["id"]
        cur = conn.execute(
            "INSERT INTO evaluations(conversation_id, checklist_id, status, created_at) VALUES (?,?, 'queued', ?)",
            (conversation_id, checklist_id, now()),
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


@app.post("/api/evaluations", status_code=202)
async def evaluation_start(body: EvaluateIn):
    if not llm.status()["configured"]:
        raise HTTPException(503, "Модель не настроена: укажите OPENAI_API_KEY в .env и перезапустите сервер.")
    with connect() as conn:
        checklist_id = _checklist(conn, body.checklist_id)["id"]
    return {"id": _start(body.conversation_id, checklist_id)}


class BatchIn(BaseModel):
    conversation_ids: list[str] = Field(default_factory=list)
    group_id: str | None = None
    only_unevaluated: bool = True
    checklist_id: int | None = None


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
    return {"started": [_start(i, checklist_id) for i in ids], "checklist_id": checklist_id}


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
