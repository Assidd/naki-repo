"""Запуск: python run.py — сервер и интерфейс на http://127.0.0.1:PORT (по умолчанию 8765)."""
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
load_dotenv(ROOT / ".env")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8765") or 8765)
    print(f"CQA: откройте http://127.0.0.1:{port}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)
