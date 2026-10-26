"""Per-day SQLite + WAV storage. One collector writer, read-only web readers."""
from contextlib import closing
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import uuid
import wave

STORAGE_LOCK = threading.RLock()
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def valid_day(value):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    dt.date.fromisoformat(value)
    return value


class Store:
    def __init__(self, root, retention_days=7):
        self.root = Path(root).resolve()
        self.retention_days = retention_days

    def expired(self, day, today=None):
        today = today or dt.date.today()
        return self.retention_days > 0 and dt.date.fromisoformat(day) < today - dt.timedelta(days=self.retention_days - 1)

    def day_dir(self, day):
        directory = self.root / valid_day(day)
        if directory.is_symlink() or directory.resolve().parent != self.root:
            raise ValueError("无效的数据目录")
        return directory

    def begin(self, started_at, pcm, prefix="meeting"):
        day = started_at.date().isoformat()
        with STORAGE_LOCK:
            if self.expired(day):
                return None
            directory = self.day_dir(day)
            audio_dir = directory / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            record_id = uuid.uuid4().hex
            filename = f"{prefix}_{started_at:%H-%M-%S-%f}_{record_id}.wav"
            audio_file = audio_dir / filename
            temporary = audio_file.with_suffix(".tmp")
            try:
                with wave.open(str(temporary), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(pcm)
                os.replace(temporary, audio_file)
                with closing(sqlite3.connect(directory / "records.sqlite3", timeout=10)) as conn, conn:
                    conn.execute("""CREATE TABLE IF NOT EXISTS records (
                        id TEXT PRIMARY KEY, started_at TEXT NOT NULL, duration REAL NOT NULL,
                        audio_file TEXT NOT NULL, status TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                        segments TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '')""")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_started ON records(started_at, id)")
                    conn.execute("INSERT INTO records(id, started_at, duration, audio_file, status) VALUES(?,?,?,?,?)",
                                 (record_id, started_at.isoformat(timespec="milliseconds"), len(pcm)/32000, filename, "pending"))
            except Exception:
                temporary.unlink(missing_ok=True)
                audio_file.unlink(missing_ok=True)
                raise
            return record_id

    def finish(self, day, record_id, segments=None, error="", payload=None):
        with STORAGE_LOCK:
            if self.expired(day) or not record_id:
                return
            segments = segments or []
            text = "\n".join(s["text"] for s in segments)
            status = "failed" if error else ("done" if text else "empty")
            directory = self.day_dir(day)
            with closing(sqlite3.connect((directory / "records.sqlite3").as_uri()+"?mode=rw", uri=True, timeout=10)) as conn, conn:
                conn.execute("UPDATE records SET status=?, text=?, segments=?, error=? WHERE id=?",
                             (status, text, json.dumps(segments, ensure_ascii=False), error, record_id))
            if payload is not None:
                raw_dir = directory / "raw_json"
                raw_dir.mkdir(exist_ok=True)
                target = raw_dir / f"{record_id}.json"
                temp = target.with_suffix(".tmp")
                try:
                    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(temp, target)
                except OSError:
                    logging.exception("保存原始 ASR JSON 失败；已归档的文本和音频仍保留")
                finally:
                    temp.unlink(missing_ok=True)

    def _read(self, day, query, parameters=()):
        database = self.day_dir(day) / "records.sqlite3"
        if not database.is_file():
            return []
        try:
            with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row
                return [dict(row) for row in conn.execute(query, parameters)]
        except sqlite3.OperationalError:
            # A retention sweep may unlink a day between exists() and connect().
            if not database.exists():
                return []
            raise

    def days(self):
        if not self.root.exists():
            return []
        result = []
        for folder in sorted(self.root.iterdir(), reverse=True):
            if DATE_RE.fullmatch(folder.name) and folder.is_dir() and not folder.is_symlink():
                try:
                    valid_day(folder.name)
                except ValueError:
                    continue
                rows = self._read(folder.name, "SELECT count(*) AS count, coalesce(sum(duration),0) AS duration FROM records")
                if rows and rows[0]["count"]:
                    result.append({"date": folder.name, **rows[0]})
        return result

    def records(self, day, query="", limit=50, offset=0):
        where = "WHERE instr(lower(text),lower(?)) > 0" if query else ""
        params = (query,) if query else ()
        summary = self._read(day, f"""SELECT count(*) AS count, coalesce(sum(duration),0) AS duration,
            coalesce(sum(status='failed'),0) AS failed, coalesce(sum(status='pending'),0) AS pending,
            coalesce(sum(status='empty'),0) AS empty FROM records {where}""", params)
        rows = self._read(day, f"SELECT * FROM records {where} ORDER BY started_at, id LIMIT ? OFFSET ?", (*params, limit, offset))
        for row in rows:
            row["segments"] = json.loads(row["segments"])
        return {"items": rows, "summary": summary[0] if summary else {"count":0,"duration":0,"failed":0,"pending":0,"empty":0}}

    def record(self, day, record_id):
        rows = self._read(day, "SELECT * FROM records WHERE id=?", (record_id,))
        return rows[0] if rows else None

    def recover_pending(self):
        with STORAGE_LOCK:
            for day in self.days():
                database = self.day_dir(day["date"]) / "records.sqlite3"
                with closing(sqlite3.connect(database, timeout=10)) as conn, conn:
                    conn.execute("UPDATE records SET status='failed', error='采集进程上次退出，识别未完成；音频已保留' WHERE status='pending'")

    def cleanup(self, today=None):
        removed = []
        if not self.retention_days or not self.root.exists():
            return removed
        with STORAGE_LOCK:
            for folder in self.root.iterdir():
                if not DATE_RE.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                    continue
                try:
                    valid_day(folder.name)
                except ValueError:
                    continue
                if self.expired(folder.name, today):
                    shutil.rmtree(self.day_dir(folder.name))
                    removed.append(folder.name)
        return sorted(removed)
