"""Сквозной сценарий API с подставной моделью (только в тестах, в рабочую базу не пишет)."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import evaluator, llm

SAMPLES = json.loads((Path(__file__).parent.parent / "samples" / "demo-dialogs.json").read_text(encoding="utf-8"))


class FakeProvider:
    name = "fake"
    model = "fake-model"
    available = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, system, user, schema, audio=None):
        self.last_audio = audio
        self.last_system = system
        self.calls += 1
        return llm.LLMReply(self.replies.pop(0), "fake-model", 100, 50)


def good_reply():
    items = [
        ("c1", "yes", [("m3", "Меня зовут Айгерим")], None),
        ("c2", "na", [], None),
        ("c3", "yes", [("m3", "подтвердите, пожалуйста, ФИО")], None),
        ("c4", "20", [("m6", "Ваша заявка на вывод 500 USD принята")], None),
        ("c5", "yes", [("m5", "Спасибо, данные подтверждены")], None),
        ("c6", "10", [("m5", "Минуту, проверяю заявку")], None),
        ("c7", "yes", [("m8", "Могу ещё чем-то помочь?")], None),
        ("c8", "no", [], None),
    ]
    return {"criteria": [
        {"criterion_id": c, "answer": a, "explanation": "ok", "evidence": [{"message_id": m, "quote": q} for m, q in ev], "checked_range": r}
        for c, a, ev, r in items
    ]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.sqlite"))
    evaluator._semaphore = None
    from app.main import app
    with TestClient(app) as c:
        yield c


def use_fake(monkeypatch, replies):
    fake = FakeProvider(replies)
    monkeypatch.setattr(llm, "provider_chain", lambda *a, **k: [fake])
    return fake


def wait_eval(client, eid):
    for _ in range(100):
        ev = client.get(f"/api/evaluations/{eid}").json()
        if ev["status"] not in ("queued", "running"):
            return ev
        time.sleep(0.05)
    raise AssertionError("оценка не завершилась")


def test_import_and_list(client):
    r = client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    assert r.status_code == 200 and r.json()["total"] == 5
    rows = client.get("/api/conversations").json()
    assert {x["id"] for x in rows} >= {"demo-ru-001", "demo-call-001"}
    conv = client.get("/api/conversations/demo-ru-001").json()
    assert conv["transcript"][0]["id"] == "m1"


def test_import_rejects_bad_records(client):
    bad = {"records": [{"conversation_id": "x", "language": "DE", "transcript": [{"speaker": "client", "text": "hi"}]}]}
    r = client.post("/api/dataset/import", json={"payload": bad})
    assert r.status_code == 400


def test_evaluate_appeal_and_recompute(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    use_fake(monkeypatch, [good_reply()])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    ev = wait_eval(client, eid)
    assert ev["status"] == "done", ev
    assert ev["score"] == "100.00" and ev["critical"] is False
    assert all(e["verified"] for r in ev["results"].values() for e in r["evidence"])

    a = client.post("/api/appeals", json={"evaluation_id": eid, "criterion_id": "c4", "comment": "Не назван точный срок"}).json()
    preview = client.post(f"/api/appeals/{a['id']}/preview", json={"decision": "change", "new_answer": "15"}).json()
    assert preview["score"] == "94.12"  # c2 N/A → вес 85; 80/85*100
    assert client.post(f"/api/appeals/{a['id']}/decide", json={"decision": "change", "new_answer": "15", "reason": ""}).status_code == 400
    done = client.post(f"/api/appeals/{a['id']}/decide", json={"decision": "change", "new_answer": "15", "reason": "Срок указан как диапазон"}).json()
    assert done["score"] == "100.00"          # исходная оценка сохранена
    assert done["final"]["score"] == "94.12"  # финальная с учётом решения


def test_invented_quote_needs_review_after_repair(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    bad = good_reply()
    bad["criteria"][0]["evidence"] = [{"message_id": "m3", "quote": "Добрый вечер, я Ольга"}]
    fake = use_fake(monkeypatch, [bad, bad])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    ev = wait_eval(client, eid)
    assert fake.calls == 2  # была попытка исправления
    assert ev["status"] == "needs_review"
    assert any("c1" in i for i in ev["issues"])


def test_repair_fixes_structure(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    broken = good_reply()
    broken["criteria"][3]["answer"] = "17"  # нет такой ступени
    use_fake(monkeypatch, [broken, good_reply()])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    assert wait_eval(client, eid)["status"] == "done"


def test_provider_error_is_not_zero_score(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})

    class Broken(FakeProvider):
        async def complete(self, *a, **k):
            raise llm.LLMError("timeout")

    monkeypatch.setattr(llm, "provider_chain", lambda *a, **k: [Broken([])])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    ev = wait_eval(client, eid)
    assert ev["status"] == "failed" and ev["score"] is None


def test_fallback_provider_is_used(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})

    class Broken(FakeProvider):
        name = "primary"

        async def complete(self, *a, **k):
            raise llm.LLMError("quota")

    backup = FakeProvider([good_reply()])
    monkeypatch.setattr(llm, "provider_chain", lambda *a, **k: [Broken([]), backup])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    ev = wait_eval(client, eid)
    assert ev["status"] == "done" and ev["model"].startswith("fake:")


def test_new_checklist_version(client):
    cl = client.get("/api/checklists/latest").json()
    data = cl["data"]
    data["criteria"][6]["title"] = "Завершение с резюме"
    data["criteria"][6]["weight"] = 10
    saved = client.post("/api/checklists", json={"data": data, "comment": "Замена критерия"}).json()
    assert saved["version"] == 2
    assert len(client.get("/api/checklists").json()) == 2
    data["criteria"][6]["weight"] = -5
    assert client.post("/api/checklists", json={"data": data}).status_code == 400


def test_quote_matching_tolerates_whitespace_and_ellipsis():
    text = "Здравствуйте!  Меня зовут Айгерим, я специалист поддержки."
    assert evaluator.quote_in_message("меня зовут  Айгерим", text)
    assert evaluator.quote_in_message("Здравствуйте! … специалист поддержки", text)
    assert not evaluator.quote_in_message("Меня зовут Ольга", text)


def test_analytics_uses_final_scores(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    use_fake(monkeypatch, [good_reply()])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    wait_eval(client, eid)
    a = client.post("/api/appeals", json={"evaluation_id": eid, "criterion_id": "c4", "comment": "x"}).json()
    client.post(f"/api/appeals/{a['id']}/decide", json={"decision": "change", "new_answer": "15", "reason": "y"})
    data = client.get("/api/analytics?group_id=DEMO").json()
    assert data["total"] == 5 and data["statuses"]["done"] == 1 and data["statuses"]["none"] == 4
    assert data["avg_score"] == 94.12
    assert data["appeals"] == {"total": 1, "open": 0, "confirmed": 0, "changed": 1}
    c4 = next(c for c in data["criteria"] if c["id"] == "c4")
    assert c4["fulfilment"] == 75.0
    c2 = next(c for c in data["criteria"] if c["id"] == "c2")
    assert c2["na"] == 1 and c2["fulfilment"] is None
    assert data["groups"] == ["DEMO"]


def test_results_export_matches_organizer_format(client, monkeypatch, tmp_path):
    monkeypatch.setenv("RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("TEAM_NAME", "naki")
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    use_fake(monkeypatch, [good_reply()])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    wait_eval(client, eid)
    a = client.post("/api/appeals", json={"evaluation_id": eid, "criterion_id": "c4", "comment": "x"}).json()
    client.post(f"/api/appeals/{a['id']}/decide", json={"decision": "change", "new_answer": "15", "reason": "y"})

    data = client.get("/api/results?group_id=DEMO").json()
    p = data["payload"]
    assert p["schema_version"] == "1.0" and p["team_name"] == "naki"
    assert p["model"] == {"mode": "external_api", "name": "fake-model"}
    assert len(p["results"]) == 5 and data["ok"] == 1
    rec = next(r for r in p["results"] if r["conversation_id"] == "demo-ru-001")
    vals = {c["criterion_id"]: c["value"] for c in rec["criteria"]}
    assert vals == {"contact": True, "discovery": None, "identification": True, "consultation": 20,
                    "empathy": True, "dialogue": 10, "closure": True, "critical": False}
    assert next(c for c in rec["criteria"] if c["criterion_id"] == "contact")["evidence_turns"] == [3]
    assert rec["score"] == {"final_score": 100.0, "critical_flag": False}  # по умолчанию — оценка модели без апелляций
    err = next(r for r in p["results"] if r["conversation_id"] == "demo-en-001")
    assert err == {"conversation_id": "demo-en-001", "status": "error", "criteria": [], "score": None, "error": "Оценка ещё не выполнена"}
    assert any("status=error" in x for x in data["problems"])

    with_appeals = client.get("/api/results?group_id=DEMO&use_appeals=true").json()["payload"]
    rec2 = next(r for r in with_appeals["results"] if r["conversation_id"] == "demo-ru-001")
    assert rec2["score"]["final_score"] == 94.12

    saved = client.post("/api/results/save", json={"group_id": "DEMO", "target": "dev"}).json()
    assert saved["path"] == "results/dev/results.json"
    on_disk = json.loads((tmp_path / "results" / "dev" / "results.json").read_text(encoding="utf-8"))
    assert on_disk == p
    assert client.post("/api/results/save", json={"group_id": "DEMO", "target": "test"}).status_code == 400


def test_model_selection(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODELS", "gpt-4o-mini,gpt-4.1")
    models = {m["id"] for m in client.get("/api/status").json()["llm"]["models"]}
    assert {"openai:gpt-4o-mini", "openai:gpt-4.1"} <= models
    chain = llm.provider_chain("openai:gpt-4.1")
    assert chain[0].name == "openai" and chain[0].model == "gpt-4.1"
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    r = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001", "model": "openai:unknown"})
    assert r.status_code == 400
    assert llm.cost_usd("openai", "gpt-4.1-2025-04-14", 1_000_000, 0) == 2.0


def test_delete_dataset(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    client.post("/api/dataset/import", json={"group_id": "KEEP", "payload": {"records": [{"conversation_id": "k1", "language": "RU", "channel": "agent chat", "transcript": [{"speaker": "client", "text": "Привет"}]}]}})
    use_fake(monkeypatch, [good_reply()])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    wait_eval(client, eid)
    client.post("/api/appeals", json={"evaluation_id": eid, "criterion_id": "c4", "comment": "x"})
    r = client.delete("/api/datasets/DEMO").json()
    assert r == {"group_id": "DEMO", "conversations": 5, "evaluations": 1}
    assert [c["id"] for c in client.get("/api/conversations").json()] == ["k1"]
    assert client.get("/api/appeals").json() == []
    assert client.delete("/api/datasets/DEMO").status_code == 404


def call_reply(with_speech=True):
    items = [
        ("c1", "yes", [("m1", "меня зовут Алексей")]), ("c2", "yes", [("m3", "какая ошибка на экране")]),
        ("c3", "na", []), ("c4", "15", [("m5", "Обновите приложение")]), ("c5", "yes", [("m7", "Чем-то ещё могу помочь")]),
        ("c6", "10", [("m5", "Обновите приложение")]), ("c7", "yes", [("m7", "Всего доброго")]), ("c8", "no", []),
    ]
    data = {"criteria": [{"criterion_id": c, "answer": a, "explanation": "ok", "checked_range": None,
                          "evidence": [{"message_id": m, "quote": q} for m, q in ev]} for c, a, ev in items]}
    if with_speech:
        data["speech"] = {"tone": "доброжелательный", "diction": "чёткая", "pace": "спокойный", "politeness": "не перебивал",
                          "summary": "Хорошо", "issues": [], "asr_corrections": [
                              {"message_id": "m5", "heard": "эс эм эс", "likely": "SMS"},
                              {"message_id": "m99", "heard": "x", "likely": "y"}]}
    return data


def test_call_is_evaluated_with_audio_by_gemini(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    from app import main
    from app.db import connect
    with connect() as conn:
        conn.execute("UPDATE conversations SET audio_url='https://example.org/call.mp3' WHERE id='demo-call-001'")
    audio_fake = FakeProvider([call_reply()])
    audio_fake.name, audio_fake.model = "gemini", "gemini-test"
    text_fake = FakeProvider([call_reply(False)])
    monkeypatch.setattr(llm, "audio_provider", lambda *a, **k: audio_fake)
    monkeypatch.setattr(llm, "provider_chain", lambda *a, **k: [text_fake])

    async def fake_download(cid, url):
        return b"ID3fake", "audio/mpeg"
    monkeypatch.setattr(main.media, "download_audio", fake_download)

    eid = client.post("/api/evaluations", json={"conversation_id": "demo-call-001"}).json()["id"]
    ev = wait_eval(client, eid)
    assert ev["status"] == "done", ev
    assert audio_fake.last_audio == (b"ID3fake", "audio/mpeg") and "АУДИОЗАПИСЬ" in audio_fake.last_system
    assert text_fake.calls == 0
    assert "+audio" in ev["model"]
    sp = ev["speech"]
    assert sp["available"] and sp["tone"] == "доброжелательный"
    assert sp["asr_corrections"] == [{"message_id": "m5", "heard": "эс эм эс", "likely": "SMS"}]  # m99 отброшен


def test_call_falls_back_to_text_when_audio_fails(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    from app import main
    from app.db import connect
    with connect() as conn:
        conn.execute("UPDATE conversations SET audio_url='https://example.org/call.mp3' WHERE id='demo-call-001'")

    class BrokenAudio(FakeProvider):
        name, model = "gemini", "gemini-test"

        async def complete(self, *a, **k):
            raise llm.LLMError("Gemini: HTTP 429")

    text_fake = FakeProvider([call_reply(False)])
    monkeypatch.setattr(llm, "audio_provider", lambda *a, **k: BrokenAudio([]))
    monkeypatch.setattr(llm, "provider_chain", lambda *a, **k: [text_fake])

    async def fake_download(cid, url):
        return b"ID3fake", "audio/mpeg"
    monkeypatch.setattr(main.media, "download_audio", fake_download)

    eid = client.post("/api/evaluations", json={"conversation_id": "demo-call-001"}).json()["id"]
    ev = wait_eval(client, eid)
    assert ev["status"] == "done" and text_fake.last_audio is None
    assert ev["speech"]["available"] is False and "429" in ev["speech"]["reason"]


def test_gemini_audio_cost():
    # 1000 текстовых + 3000 аудио-токенов на входе, 500 на выходе для gemini-2.5-flash
    assert llm.cost_usd("gemini", "gemini-2.5-flash", 4000, 500, 3000) == round(1000 / 1e6 * 0.30 + 3000 / 1e6 * 1.00 + 500 / 1e6 * 2.50, 6)


def test_kazakh_explanation_triggers_repair(client, monkeypatch):
    client.post("/api/dataset/import", json={"group_id": "DEMO", "payload": SAMPLES})
    kz = good_reply()
    kz["criteria"][0]["explanation"] = "Оператор амандасып, өзін таныстырды."
    fake = use_fake(monkeypatch, [kz, good_reply()])
    eid = client.post("/api/evaluations", json={"conversation_id": "demo-ru-001"}).json()["id"]
    ev = wait_eval(client, eid)
    assert fake.calls == 2 and ev["status"] == "done"
    assert ev["results"]["c1"]["explanation"] == "ok"
