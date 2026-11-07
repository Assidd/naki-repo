"""Импорт диалогов из файлов разных форматов.

Поддерживается:
  .json        — формат API организаторов, объект {records: [...]}, массив записей или одна запись;
  .csv / .tsv  — таблица «одна строка = одна реплика»;
  .xlsx        — та же таблица в Excel (все листы);
  .txt         — текст диалога построчно: «Оператор: …», «[00:12] Клиент: …».

Все форматы приводятся к записям организаторов и затем проходят общую проверку dataset.normalize_record.
"""
import csv
import io
import json
import re
from pathlib import PurePath

from .dataset import DatasetError, extract_records

SUPPORTED = (".json", ".csv", ".tsv", ".xlsx", ".txt")

# Названия столбцов, которые понимаем (регистр и пробелы не важны)
COLUMNS = {
    "conversation_id": {"conversation_id", "conversation", "dialog_id", "dialogue_id", "dialog", "chat_id", "call_id", "id", "диалог", "id диалога", "номер диалога", "чат"},
    "channel": {"channel", "канал", "тип"},
    "language": {"language", "lang", "язык"},
    "timestamp": {"timestamp", "time", "datetime", "date", "время", "дата", "метка времени"},
    "speaker": {"speaker", "role", "author", "from", "sender", "роль", "автор", "кто", "отправитель", "участник"},
    "text": {"text", "message", "content", "utterance", "текст", "сообщение", "реплика"},
    "audio_url": {"audio_url", "audio", "recording", "record_url", "запись", "аудио", "ссылка на запись"},
}

SPEAKERS = {
    "operator": {"operator", "agent", "оператор", "агент", "специалист", "сотрудник", "менеджер", "консультант", "support"},
    "client": {"client", "customer", "user", "клиент", "пользователь", "абонент", "покупатель"},
    "bot": {"bot", "бот", "assistant", "ассистент", "ai"},
    "system": {"system", "система", "системное сообщение"},
}

CHANNELS = {
    "agent chat": {"agent chat", "chat", "чат", "переписка", "текст"},
    "voice call": {"voice call", "call", "voice", "звонок", "телефон", "голос"},
}

LANGS = {"RU": {"ru", "rus", "русский", "ру"}, "KZ": {"kz", "kk", "kaz", "казахский", "қазақ", "қазақша"}, "EN": {"en", "eng", "english", "английский"}}

KZ_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def map_speaker(value) -> str | None:
    v = _norm(value)
    for role, names in SPEAKERS.items():
        if v in names:
            return role
    return None


def map_channel(value, default: str) -> str:
    v = _norm(value)
    for ch, names in CHANNELS.items():
        if v in names:
            return ch
    return default


def map_language(value) -> str | None:
    v = _norm(value)
    for lang, names in LANGS.items():
        if v in names:
            return lang
    return None


def detect_language(texts: list[str]) -> str:
    """Простое определение: казахские буквы → KZ, кириллица → RU, иначе EN."""
    joined = " ".join(texts)
    if any(ch in KZ_LETTERS for ch in joined):
        return "KZ"
    cyr = sum("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in joined)
    lat = sum("a" <= ch.lower() <= "z" for ch in joined)
    return "RU" if cyr >= lat else "EN"


def _finish(record: dict, default_language: str, default_channel: str) -> dict:
    if not record.get("language"):
        record["language"] = default_language if default_language != "AUTO" else detect_language([m["text"] for m in record["transcript"]])
    if not record.get("channel"):
        record["channel"] = default_channel
    return record


# ---------- таблицы ----------

def _header_map(header: list) -> dict:
    found = {}
    for idx, name in enumerate(header):
        n = _norm(name)
        for key, names in COLUMNS.items():
            if n in names and key not in found:
                found[key] = idx
    missing = [k for k in ("speaker", "text") if k not in found]
    if missing:
        raise DatasetError(
            "В таблице не найдены обязательные столбцы: " + ", ".join(missing)
            + ". Нужны как минимум «speaker» (роль) и «text» (текст реплики); см. шаблон CSV."
        )
    return found


def rows_to_records(rows: list[list], fallback_id: str, default_language: str, default_channel: str) -> list[dict]:
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if len(rows) < 2:
        raise DatasetError("Таблица пуста: нужна строка заголовков и хотя бы одна реплика")
    cols = _header_map(rows[0])
    get = lambda r, k: (str(r[cols[k]]).strip() if k in cols and cols[k] < len(r) and r[cols[k]] is not None else "")
    records: dict[str, dict] = {}
    for line_no, r in enumerate(rows[1:], start=2):
        cid = get(r, "conversation_id") or fallback_id
        rec = records.setdefault(cid, {"conversation_id": cid, "channel": "", "language": "", "transcript": [], "audio_url": None})
        if get(r, "channel") and not rec["channel"]:
            rec["channel"] = map_channel(get(r, "channel"), default_channel)
        if get(r, "language") and not rec["language"]:
            lang = map_language(get(r, "language"))
            if not lang:
                raise DatasetError(f"Строка {line_no}: неизвестный язык «{get(r, 'language')}» (нужно RU, KZ или EN)")
            rec["language"] = lang
        if get(r, "audio_url") and not rec["audio_url"]:
            rec["audio_url"] = get(r, "audio_url")
        text = get(r, "text")
        if not text:
            continue
        speaker = map_speaker(get(r, "speaker"))
        if not speaker:
            raise DatasetError(f"Строка {line_no}: неизвестная роль «{get(r, 'speaker')}» (оператор, клиент, бот или система)")
        rec["transcript"].append({"timestamp": get(r, "timestamp"), "speaker": speaker, "text": text})
    return [_finish(rec, default_language, default_channel) for rec in records.values() if rec["transcript"]]


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DatasetError("Не удалось прочитать файл: сохраните его в кодировке UTF-8")


def parse_csv(data: bytes, fallback_id: str, default_language: str, default_channel: str, tsv: bool = False) -> list[dict]:
    text = _decode(data)
    if tsv:
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return rows_to_records(rows, fallback_id, default_language, default_channel)


def parse_xlsx(data: bytes, fallback_id: str, default_language: str, default_channel: str) -> list[dict]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise DatasetError("Не удалось открыть файл Excel (.xlsx)") from None
    records = []
    for ws in wb.worksheets:
        rows = [["" if v is None else v for v in row] for row in ws.iter_rows(values_only=True)]
        if len([r for r in rows if any(str(c).strip() for c in r)]) < 2:
            continue
        # Время в Excel может прийти как datetime — оставляем ISO-строкой
        rows = [[v.isoformat() if hasattr(v, "isoformat") else v for v in r] for r in rows]
        records += rows_to_records(rows, f"{fallback_id}-{ws.title}" if len(wb.worksheets) > 1 else fallback_id, default_language, default_channel)
    if not records:
        raise DatasetError("В файле Excel нет листа с репликами")
    return records


# ---------- текст ----------

LINE = re.compile(r"^\s*(?:\[(?P<ts1>[^\]]+)\]\s*|(?P<ts2>\d{1,2}:\d{2}(?::\d{2})?)\s+)?(?P<who>[^:\[\]]{1,40}?)\s*:\s*(?P<text>.*)$")
HEADER = re.compile(r"^\s*(?:#{2,}|={3,})\s*(?P<id>[^=#]+?)\s*(?:={3,})?\s*$")


def parse_txt(data: bytes, fallback_id: str, default_language: str, default_channel: str) -> list[dict]:
    """Строки «[время] Роль: текст» или «Роль: текст». Строка без роли продолжает предыдущую реплику.
    Несколько диалогов в одном файле разделяются заголовками «### id» или «=== id ===»."""
    records, current = [], None
    for raw in _decode(data).splitlines():
        if not raw.strip():
            continue
        h = HEADER.match(raw)
        if h:
            current = {"conversation_id": h.group("id").strip(), "channel": "", "language": "", "transcript": [], "audio_url": None}
            records.append(current)
            continue
        m = LINE.match(raw)
        speaker = map_speaker(m.group("who")) if m else None
        if current is None:
            current = {"conversation_id": fallback_id, "channel": "", "language": "", "transcript": [], "audio_url": None}
            records.append(current)
        if speaker:
            current["transcript"].append({"timestamp": (m.group("ts1") or m.group("ts2") or "").strip(), "speaker": speaker, "text": m.group("text").strip()})
        elif current["transcript"]:
            current["transcript"][-1]["text"] += "\n" + raw.strip()
        else:
            raise DatasetError(f"Не понятна строка «{raw.strip()[:60]}». Начинайте реплики с роли: «Оператор: …», «Клиент: …»")
    records = [r for r in records if r["transcript"]]
    if not records:
        raise DatasetError("В текстовом файле не найдено ни одной реплики")
    return [_finish(r, default_language, default_channel) for r in records]


# ---------- общий вход ----------

def parse_file(filename: str, data: bytes, default_language: str = "AUTO", default_channel: str = "agent chat") -> list[dict]:
    if not data:
        raise DatasetError("Файл пустой")
    name = PurePath(filename or "dialog.txt")
    ext = name.suffix.lower()
    fallback_id = re.sub(r"[^\w\-]+", "-", name.stem).strip("-") or "dialog"
    default_language = (default_language or "AUTO").upper()
    if ext == ".json":
        try:
            payload = json.loads(_decode(data))
        except ValueError:
            raise DatasetError("Файл JSON повреждён") from None
        if isinstance(payload, dict) and "transcript" in payload:
            payload = [payload]
        records = extract_records(payload)
        out = []
        for r in records:
            if isinstance(r, dict):
                r = dict(r)
                r.setdefault("conversation_id", fallback_id)
                lang = map_language(r.get("language"))
                r["language"] = lang or ""
                r["channel"] = map_channel(r.get("channel"), default_channel) if r.get("channel") else ""
                if isinstance(r.get("transcript"), list):
                    r["transcript"] = [{**m, "speaker": map_speaker(m.get("speaker")) or m.get("speaker")} if isinstance(m, dict) else m for m in r["transcript"]]
                    out.append(_finish(r, default_language, default_channel))
                    continue
            out.append(r)
        return out
    if ext in (".csv", ".tsv"):
        return parse_csv(data, fallback_id, default_language, default_channel, tsv=ext == ".tsv")
    if ext == ".xlsx":
        return parse_xlsx(data, fallback_id, default_language, default_channel)
    if ext == ".txt":
        return parse_txt(data, fallback_id, default_language, default_channel)
    raise DatasetError(f"Формат «{ext or 'без расширения'}» не поддерживается. Можно: {', '.join(SUPPORTED)}")


CSV_TEMPLATE = (
    "conversation_id,channel,language,timestamp,speaker,text,audio_url\n"
    "chat-001,chat,RU,00:00,client,Здравствуйте! Как пополнить счёт?,\n"
    "chat-001,chat,RU,00:40,operator,\"Здравствуйте! Меня зовут Анна. Пополнить счёт можно в приложении: Кошелёк → Пополнить.\",\n"
    "chat-001,chat,RU,01:10,client,Спасибо!,\n"
    "chat-001,chat,RU,01:20,operator,Пожалуйста! Чем-то ещё помочь? Хорошего дня!,\n"
)
