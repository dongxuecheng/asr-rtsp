"""Per-day SQLite, WAV and UTF-8 transcript storage; read-only web readers."""
from contextlib import closing
import datetime as dt
import hashlib
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
VISIBLE_RECORD_SQL = "status='done' AND length(trim(text, char(9)||char(10)||char(13)||' ')) > 0"


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

    def save(self, started_at, pcm, segments, prefix="meeting", payload=None):
        """Persist only a successful ASR result with nonblank text and its audio."""
        segments = [{**s, "text": s["text"].strip()} for s in segments
                    if isinstance(s.get("text"), str) and s["text"].strip()]
        if not segments:
            return None
        text = "\n".join(s["text"] for s in segments)
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
                    conn.execute("INSERT INTO records(id, started_at, duration, audio_file, status, text, segments) VALUES(?,?,?,?,?,?,?)",
                                 (record_id, started_at.isoformat(timespec="milliseconds"), len(pcm)/32000,
                                  filename, "done", text, json.dumps(segments, ensure_ascii=False)))
            except Exception:
                temporary.unlink(missing_ok=True)
                audio_file.unlink(missing_ok=True)
                raise
            try:
                self._append_transcript(day, record_id)
            except (OSError, ValueError, sqlite3.Error):
                logging.exception("写入转写 TXT 失败；文本和音频已归档，后续同步会修复：%s", day)
            if payload is not None:
                raw_dir = directory / "raw_json"
                target = raw_dir / f"{record_id}.json"
                temp = target.with_suffix(".tmp")
                try:
                    raw_dir.mkdir(exist_ok=True)
                    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(temp, target)
                except OSError:
                    logging.exception("保存原始 ASR JSON 失败；已归档的文本和音频仍保留")
                finally:
                    temp.unlink(missing_ok=True)
            return record_id

    @staticmethod
    def _transcript_header(day, timezone=None):
        timezone = timezone or os.getenv("TZ", "Asia/Shanghai")
        return f"{day} ASR 记录（{timezone}）\n"

    @staticmethod
    def _format_transcript_record(row):
        started = dt.datetime.fromisoformat(row["started_at"])
        lines = []
        segments = json.loads(row["segments"])
        if not segments:
            segments = [{"start":0, "end":row["duration"], "speaker":"未知说话人", "text":row["text"]}]
        for segment in segments:
            begin = started + dt.timedelta(seconds=segment.get("start", 0))
            end = started + dt.timedelta(seconds=segment.get("end", row["duration"]))
            begin_text = begin.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            end_text = end.strftime("%H:%M:%S.%f")[:-3]
            lines.append(f"[{begin_text} - {end_text}] "
                         f"[{segment.get('speaker', '未知说话人')}] {segment['text']}\n")
        return "".join(lines)

    def transcript_text(self, day, timezone=None):
        rows = self._read(day, f"SELECT * FROM records WHERE {VISIBLE_RECORD_SQL} ORDER BY started_at, id")
        return self._transcript_header(day, timezone) + "".join(self._format_transcript_record(row) for row in rows)

    def transcript_path(self, day):
        directory = self.day_dir(day)
        target = directory / f"asr_{day}.txt"
        if target.is_symlink() or target.resolve().parent != directory:
            raise ValueError("无效的转写 TXT 路径")
        return target

    def _append_transcript(self, day, record_id):
        target = self.transcript_path(day)
        if not target.exists():
            # Also include older database-only records on the first write.
            self._sync_transcript(day)
            return
        row = self.record(day, record_id)
        if row:
            with target.open("a", encoding="utf-8", newline="\n") as file:
                file.write(self._format_transcript_record(row))

    def _sync_transcript(self, day):
        """Atomically rebuild a day's TXT from the canonical database."""
        target = self.transcript_path(day)
        rows = self._read(day, f"SELECT * FROM records WHERE {VISIBLE_RECORD_SQL} ORDER BY started_at, id")
        if not rows:
            target.unlink(missing_ok=True)
            return
        content = self._transcript_header(day) + "".join(self._format_transcript_record(row) for row in rows)
        try:
            if target.read_text(encoding="utf-8") == content:
                return
        except (FileNotFoundError, UnicodeError):
            pass
        temporary = target.with_suffix(".txt.tmp")
        if temporary.is_symlink() or temporary.resolve().parent != target.parent:
            raise ValueError("无效的转写 TXT 临时路径")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def sync_transcripts(self):
        """Backfill or repair retained daily transcripts on startup and cleanup."""
        with STORAGE_LOCK:
            for day in self._day_names():
                if self.expired(day):
                    continue
                try:
                    self._sync_transcript(day)
                except (OSError, ValueError, sqlite3.Error):
                    logging.exception("同步转写 TXT 失败，将在下轮清理时重试：%s", day)

    def _discard_unrecognized(self, day, record_id):
        """Remove files before the row, so interrupted purges remain retryable."""
        try:
            directory = self.day_dir(day)
            rows = self._read(day, f"SELECT audio_file FROM records WHERE id=? AND NOT ({VISIBLE_RECORD_SQL})", (record_id,))
            if not rows:
                return True
            audio_dir = directory / "audio"
            filename = rows[0]["audio_file"]
            audio = audio_dir / filename
            raw_dir = directory / "raw_json"
            if (not re.fullmatch(r"[0-9a-f]{32}", record_id)
                    or Path(filename).name != filename
                    or audio_dir.is_symlink() or audio_dir.resolve().parent != directory
                    or audio.resolve().parent != audio_dir.resolve()
                    or raw_dir.is_symlink() or raw_dir.resolve().parent != directory):
                raise ValueError("拒绝清理数据目录之外的音频或响应文件")
            audio.unlink(missing_ok=True)
            (raw_dir / f"{record_id}.json").unlink(missing_ok=True)
            (raw_dir / f"{record_id}.tmp").unlink(missing_ok=True)
            with closing(sqlite3.connect((directory / "records.sqlite3").as_uri()+"?mode=rw", uri=True, timeout=10)) as conn, conn:
                conn.execute(f"DELETE FROM records WHERE id=? AND NOT ({VISIBLE_RECORD_SQL})", (record_id,))
            return True
        except (OSError, ValueError, sqlite3.Error):
            logging.exception("删除未成功转写的录音失败，记录已隐藏，将在后续清理时重试：%s/%s", day, record_id)
            return False

    def purge_unrecognized(self):
        """Purge legacy failed, pending and empty records, even with retention off."""
        removed = 0
        with STORAGE_LOCK:
            for day in self._day_names():
                invalid = self._read(day, f"SELECT id FROM records WHERE NOT ({VISIBLE_RECORD_SQL})")
                for row in invalid:
                    removed += int(self._discard_unrecognized(day, row["id"]))
                if invalid:
                    try:
                        self._sync_transcript(day)
                    except (OSError, ValueError, sqlite3.Error):
                        logging.exception("清理历史无效记录后同步 TXT 失败：%s", day)
        return removed

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

    def _day_names(self):
        if not self.root.exists():
            return []
        result = []
        for folder in sorted(self.root.iterdir(), reverse=True):
            if DATE_RE.fullmatch(folder.name) and folder.is_dir() and not folder.is_symlink():
                try:
                    valid_day(folder.name)
                except ValueError:
                    continue
                result.append(folder.name)
        return result

    def days(self):
        result = []
        for day in self._day_names():
            rows = self._read(day, f"SELECT count(*) AS count, coalesce(sum(duration),0) AS duration FROM records WHERE {VISIBLE_RECORD_SQL}")
            if rows and rows[0]["count"]:
                result.append({"date": day, **rows[0]})
        return result

    def records(self, day, query="", limit=50, offset=0):
        where = f"WHERE {VISIBLE_RECORD_SQL}"
        if query:
            where += " AND instr(lower(text),lower(?)) > 0"
        params = (query,) if query else ()
        if limit is None:
            rows = self._read(day, f"SELECT * FROM records {where} ORDER BY started_at, id", params)
            summary = [{"count":len(rows), "duration":sum(r["duration"] for r in rows),
                        "characters":sum(len(r["text"]) for r in rows)}]
        else:
            summary = self._read(day, f"""SELECT count(*) AS count, coalesce(sum(duration),0) AS duration,
                coalesce(sum(length(text)),0) AS characters
                FROM records {where}""", params)
            rows = self._read(day, f"SELECT * FROM records {where} ORDER BY started_at, id LIMIT ? OFFSET ?", (*params, limit, offset))
        for row in rows:
            row["segments"] = json.loads(row["segments"])
        return {"items": rows, "summary": summary[0] if summary else {"count":0,"duration":0,"characters":0}}

    def records_revision(self, day, query=""):
        """Cheap conditional-poll token; include WAL metadata if enabled later."""
        directory = self.day_dir(day)
        signature = [day, query]
        for filename in ("records.sqlite3", "records.sqlite3-wal"):
            try:
                stat = (directory / filename).stat()
                signature.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                signature.append(None)
        return hashlib.sha256(repr(signature).encode("utf-8")).hexdigest()

    def record(self, day, record_id):
        rows = self._read(day, f"SELECT * FROM records WHERE id=? AND {VISIBLE_RECORD_SQL}", (record_id,))
        return rows[0] if rows else None

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
