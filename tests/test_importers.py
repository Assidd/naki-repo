import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.dataset import DatasetError, normalize_record
from app.importers import parse_file

SAMPLES = Path(__file__).parent.parent / "samples"


def test_csv_with_russian_headers_and_semicolons():
    recs = parse_file("demo-dialogs.csv", (SAMPLES / "demo-dialogs.csv").read_bytes())
    assert [r["conversation_id"] for r in recs] == ["csv-ru-001", "csv-en-001"]
    ru = normalize_record(recs[0])
    assert ru["language"] == "RU" and ru["channel"] == "agent chat"
    assert ru["transcript"][1]["speaker"] == "operator" and ru["transcript"][1]["timestamp"] == "00:35"
    en = normalize_record(recs[1])
    assert [m["speaker"] for m in en["transcript"]] == ["client", "operator", "system"]


def test_txt_multiple_dialogs_language_detection_and_continuation():
    recs = parse_file("demo-dialogs.txt", (SAMPLES / "demo-dialogs.txt").read_bytes())
    assert [r["conversation_id"] for r in recs] == ["txt-kz-001", "txt-ru-002"]
    assert recs[0]["language"] == "KZ" and recs[1]["language"] == "RU"
    assert recs[0]["transcript"][0]["timestamp"] == "00:00"
    assert "либо повторить перевод завтра" in recs[1]["transcript"][3]["text"]
    for r in recs:
        normalize_record(r)


def test_txt_without_header_uses_filename_and_defaults():
    recs = parse_file("Звонок 15.txt", "Agent: Hello\nCustomer: Hi".encode(), "AUTO", "voice call")
    assert recs[0]["conversation_id"] == "Звонок-15"
    assert recs[0]["language"] == "EN" and recs[0]["channel"] == "voice call"


def test_xlsx_import():
    wb = Workbook()
    ws = wb.active
    ws.append(["conversation_id", "speaker", "text", "timestamp"])
    ws.append(["x-1", "Клиент", "Сколько стоит обслуживание?", "00:01"])
    ws.append(["x-1", "Оператор", "Здравствуйте, я Ира. Обслуживание бесплатное.", "00:30"])
    buf = io.BytesIO()
    wb.save(buf)
    recs = parse_file("dialogs.xlsx", buf.getvalue(), "RU")
    assert recs[0]["conversation_id"] == "x-1" and len(recs[0]["transcript"]) == 2
    assert normalize_record(recs[0])["language"] == "RU"


def test_json_single_record_and_aliases():
    data = '{"conversation_id": "j1", "language": "ru", "channel": "chat", "transcript": [{"speaker": "Оператор", "text": "Привет"}]}'
    rec = normalize_record(parse_file("one.json", data.encode())[0])
    assert rec["language"] == "RU" and rec["channel"] == "agent chat" and rec["transcript"][0]["speaker"] == "operator"


@pytest.mark.parametrize("name, data, message", [
    ("a.csv", "who,what\nx,y".encode(), "обязательные столбцы"),
    ("a.csv", "speaker,text\nменеджер-бот,привет".encode(), "неизвестная роль"),
    ("a.txt", "просто текст без ролей".encode(), "Не понятна строка"),
    ("a.docx", b"x", "не поддерживается"),
    ("a.xlsx", b"not excel", "Excel"),
])
def test_bad_files_are_rejected_with_clear_message(name, data, message):
    with pytest.raises(DatasetError, match=message):
        parse_file(name, data)


def test_cp1251_csv():
    data = "роль,текст\nклиент,Привет\nоператор,Здравствуйте".encode("cp1251")
    recs = parse_file("old.csv", data)
    assert recs[0]["transcript"][1]["text"] == "Здравствуйте"


def test_upload_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "u.sqlite"))
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/dataset/upload?filename=demo-dialogs.txt&group_id=FILES", content=(SAMPLES / "demo-dialogs.txt").read_bytes())
        assert r.status_code == 200 and r.json()["total"] == 2
        bad = client.post("/api/dataset/upload?filename=x.pdf", content=b"%PDF")
        assert bad.status_code == 400 and "x.pdf" in bad.json()["detail"]
        assert client.get("/api/dataset/template.csv").status_code == 200
        groups = {c["group_id"] for c in client.get("/api/conversations").json()}
        assert groups == {"FILES"}
