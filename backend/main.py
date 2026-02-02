from __future__ import annotations

import csv
import io
import os
import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import FastAPI, File, UploadFile, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.getenv("DB_PATH", "vocatype_v2.db")
DEFAULT_USER_ID = os.getenv("DEFAULT_USER_ID", "demo_user")

REQUIRED_HEADERS = ["base_word", "target_pos", "use_word", "zh_meaning"]
NON_ANSWER_MARKERS = {"—", "-", "", "–"}

app = FastAPI(title="vocatype-demo v2 (use_word)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP: 放寬；正式上線請收斂
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SCHEMA_SQL = None


def norm(s: Optional[str]) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def norm_pos(s: str) -> str:
    s = norm(s).lower()
    mapping = {"adj": "adjective", "adv": "adverb", "n": "noun", "v": "verb"}
    return mapping.get(s, s)


def is_answerable(use_word: str) -> bool:
    return norm(use_word) not in NON_ANSWER_MARKERS


def detect_delimiter(sample: str) -> str:
    if "\t" in sample:
        return "\t"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=[",", "\t", ";"])
        return dialect.delimiter
    except Exception:
        return ","


async def init_db():
    global SCHEMA_SQL
    if SCHEMA_SQL is None:
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        with open(schema_path, "r", encoding="utf-8") as f:
            SCHEMA_SQL = f.read()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()


@app.on_event("startup")
async def on_startup():
    await init_db()


@app.get("/health")
async def health():
    return {"status": "ok", "db": DB_PATH}


@app.post("/import")
async def import_file(
    file: UploadFile = File(...),
    user_id: str = Query(DEFAULT_USER_ID),
):
    raw = await file.read()
    text = raw.decode("utf-8-sig", errors="replace")

    sample = text[:4096]
    delim = detect_delimiter(sample)

    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = [h.strip() for h in (reader.fieldnames or [])]

    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        return JSONResponse(
            status_code=400,
            content={
                "error": "missing_required_headers",
                "missing": missing,
                "got_headers": headers,
            },
        )

    upload_date = date.today().isoformat()
    source_file = file.filename

    total_rows = 0
    inserted_rows = 0
    skipped_rows = 0
    not_answerable_rows = 0
    failed_rows: List[Dict[str, Any]] = []
    to_insert: List[Dict[str, Any]] = []

    for row in reader:
        total_rows += 1
        base_word = norm(row.get("base_word"))
        target_pos = norm_pos(row.get("target_pos") or "")
        use_word = norm(row.get("use_word"))
        zh_meaning = norm(row.get("zh_meaning"))

        if not base_word or not target_pos or not use_word:
            failed_rows.append({"row": total_rows, "reason": "missing base_word/target_pos/use_word"})
            continue

        if not zh_meaning:
            failed_rows.append({"row": total_rows, "reason": "missing zh_meaning"})
            continue

        ans_ok = is_answerable(use_word)
        if not ans_ok:
            not_answerable_rows += 1

        to_insert.append(
            {
                "base_word": base_word,
                "target_pos": target_pos,
                "use_word": use_word,
                "ipa": norm(row.get("ipa")),
                "zh_meaning": zh_meaning,
                "example_en": norm(row.get("example_en")),
                "example_zh": norm(row.get("example_zh")),
                "phrases": norm(row.get("phrases")),
                "note": norm(row.get("note")),
                "upload_date": upload_date,
                "source_file": source_file,
                "is_answerable": 1 if ans_ok else 0,
            }
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        for item in to_insert:
            try:
                cur = await db.execute(
                    """
                    INSERT INTO voca_item
                    (base_word, target_pos, use_word, ipa, zh_meaning, example_en, example_zh, phrases, note,
                     upload_date, source_file, is_answerable)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["base_word"],
                        item["target_pos"],
                        item["use_word"],
                        item["ipa"],
                        item["zh_meaning"],
                        item["example_en"],
                        item["example_zh"],
                        item["phrases"],
                        item["note"],
                        item["upload_date"],
                        item["source_file"],
                        item["is_answerable"],
                    ),
                )
                voca_item_id = cur.lastrowid
                inserted_rows += 1

                if item["is_answerable"] == 1:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO user_item_stat
                        (user_id, voca_item_id, total_attempts, wrong_count, correct_count, error_rate, last_seen_date)
                        VALUES (?, ?, 0, 0, 0, 0, NULL)
                        """,
                        (user_id, voca_item_id),
                    )
            except aiosqlite.IntegrityError:
                skipped_rows += 1

        await db.commit()

    return {
        "status": "ok",
        "user_id": user_id,
        "source_file": source_file,
        "delimiter": "\\t" if delim == "\t" else delim,
        "upload_date": upload_date,
        "total_rows": total_rows,
        "inserted_rows": inserted_rows,
        "skipped_rows": skipped_rows,
        "not_answerable_rows": not_answerable_rows,
        "failed_rows_count": len(failed_rows),
        "failed_rows_sample": failed_rows[:20],
    }


def calc_priority(error_rate: float, is_new: int, days_since_seen: int) -> float:
    W1, W2, W3 = 10.0, 6.0, 1.0
    return (error_rate * W1) + (is_new * W2) + (days_since_seen * W3)


@app.get("/quiz/top30")
async def quiz_top30(user_id: str = Query(DEFAULT_USER_ID)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")

        rows = await db.execute_fetchall(
            """
            SELECT
              vi.id AS voca_item_id,
              vi.base_word, vi.target_pos, vi.use_word,
              vi.ipa, vi.zh_meaning, vi.example_en, vi.example_zh, vi.phrases, vi.note,
              uis.total_attempts, uis.wrong_count, uis.correct_count, uis.error_rate, uis.last_seen_date
            FROM voca_item vi
            JOIN user_item_stat uis
              ON uis.voca_item_id = vi.id
             AND uis.user_id = ?
            WHERE vi.is_answerable = 1
            """,
            (user_id,),
        )

    items: List[Dict[str, Any]] = []
    for r in rows:
        total_attempts = int(r["total_attempts"])
        error_rate = float(r["error_rate"]) if total_attempts > 0 else 1.0
        is_new = 1 if total_attempts == 0 else 0

        last_seen = r["last_seen_date"]
        if last_seen:
            try:
                d0 = datetime.fromisoformat(last_seen).date()
                days_since = (date.today() - d0).days
            except Exception:
                days_since = 0
        else:
            days_since = 999

        score = calc_priority(error_rate, is_new, days_since)

        items.append(
            {
                "voca_item_id": r["voca_item_id"],
                "prompt": {
                    "zh_meaning": r["zh_meaning"],
                    "base_word": r["base_word"],
                    "target_pos": r["target_pos"],
                    "ipa": r["ipa"],
                    "example_en": r["example_en"],
                    "example_zh": r["example_zh"],
                    "phrases": r["phrases"],
                    "note": r["note"],
                },
                "meta": {
                    "total_attempts": total_attempts,
                    "wrong_count": int(r["wrong_count"]),
                    "correct_count": int(r["correct_count"]),
                    "error_rate_effective": error_rate,
                    "last_seen_date": r["last_seen_date"],
                    "priority_score": score,
                },
            }
        )

    items.sort(key=lambda x: x["meta"]["priority_score"], reverse=True)
    return {"status": "ok", "user_id": user_id, "count": min(30, len(items)), "items": items[:30]}


@app.post("/quiz/answer")
async def quiz_answer(payload: Dict[str, Any]):
    user_id = payload.get("user_id") or DEFAULT_USER_ID
    voca_item_id = int(payload["voca_item_id"])
    user_answer = norm(str(payload.get("user_answer", ""))).lower()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")

        item = await db.execute_fetchone(
            "SELECT id, use_word FROM voca_item WHERE id = ?",
            (voca_item_id,),
        )
        if not item:
            return JSONResponse(status_code=404, content={"error": "item_not_found"})

        correct = norm(item["use_word"]).lower()
        is_correct = (user_answer == correct)

        await db.execute(
            """
            INSERT OR IGNORE INTO user_item_stat
            (user_id, voca_item_id, total_attempts, wrong_count, correct_count, error_rate, last_seen_date)
            VALUES (?, ?, 0, 0, 0, 0, NULL)
            """,
            (user_id, voca_item_id),
        )

        if is_correct:
            await db.execute(
                """
                UPDATE user_item_stat
                SET total_attempts = total_attempts + 1,
                    correct_count = correct_count + 1,
                    last_seen_date = ?
                WHERE user_id = ? AND voca_item_id = ?
                """,
                (datetime.now().isoformat(timespec="seconds"), user_id, voca_item_id),
            )
        else:
            await db.execute(
                """
                UPDATE user_item_stat
                SET total_attempts = total_attempts + 1,
                    wrong_count = wrong_count + 1,
                    last_seen_date = ?
                WHERE user_id = ? AND voca_item_id = ?
                """,
                (datetime.now().isoformat(timespec="seconds"), user_id, voca_item_id),
            )

        row = await db.execute_fetchone(
            "SELECT total_attempts, wrong_count FROM user_item_stat WHERE user_id=? AND voca_item_id=?",
            (user_id, voca_item_id),
        )
        total_attempts = int(row["total_attempts"])
        wrong_count = int(row["wrong_count"])
        error_rate = wrong_count / total_attempts if total_attempts else 0.0

        await db.execute(
            "UPDATE user_item_stat SET error_rate=? WHERE user_id=? AND voca_item_id=?",
            (error_rate, user_id, voca_item_id),
        )
        await db.commit()

    return {"status": "ok", "user_id": user_id, "voca_item_id": voca_item_id, "is_correct": is_correct, "correct_answer": correct, "error_rate": error_rate}
