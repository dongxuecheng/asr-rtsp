"""Read-only archive API and browser UI (the collector owns all writes)."""
import datetime as dt
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3

from flask import Flask, Response, abort, jsonify, request, send_file

from storage import Store, valid_day, VISIBLE_RECORD_SQL


def create_app(config=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(DATA_DIR=os.getenv("DATA_DIR", "./data"),
                      WEB_USERNAME=os.getenv("WEB_USERNAME", ""), WEB_PASSWORD=os.getenv("WEB_PASSWORD", ""),
                      RETENTION_DAYS=int(os.getenv("RETENTION_DAYS", "7")),
                      ARCHIVE_TIMEZONE=os.getenv("TZ", "Asia/Shanghai"))
    if config:
        app.config.update(config)
    if bool(app.config["WEB_USERNAME"]) != bool(app.config["WEB_PASSWORD"]):
        raise ValueError("WEB_USERNAME 与 WEB_PASSWORD 必须同时设置，或同时留空")
    store = Store(app.config["DATA_DIR"])

    @app.before_request
    def authenticate():
        if request.path == "/healthz" or not app.config["WEB_PASSWORD"]:
            return None
        auth = request.authorization
        if not auth or not hmac.compare_digest((auth.username or "").encode(), app.config["WEB_USERNAME"].encode()) or not hmac.compare_digest((auth.password or "").encode(), app.config["WEB_PASSWORD"].encode()):
            return Response("请登录", 401, {"WWW-Authenticate": 'Basic realm="RTSP ASR", charset="UTF-8"'})

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    def day_arg():
        try:
            return valid_day(request.args.get("date", dt.date.today().isoformat()))
        except ValueError:
            abort(400, description="无效日期，请使用 YYYY-MM-DD")

    @app.errorhandler(400)
    @app.errorhandler(404)
    def request_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(sqlite3.Error)
    def storage_error(error):
        app.logger.exception("读取归档数据库失败")
        return jsonify(error="归档暂时不可用，请稍后重试"), 503

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/days")
    def days():
        return jsonify(days=store.days(), today=dt.date.today().isoformat(),
                       retention_days=app.config["RETENTION_DAYS"], timezone=app.config["ARCHIVE_TIMEZONE"])

    @app.get("/api/records")
    def records():
        day = day_arg()
        try:
            page = int(request.args.get("page", "1"))
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            abort(400, description="分页参数应为整数")
        if page < 1 or not 1 <= limit <= 200:
            abort(400, description="page 应大于 0，limit 应在 1～200 之间")
        result = store.records(day, request.args.get("q", "")[:500], limit, (page-1)*limit)
        for row in result["items"]:
            row.pop("audio_file", None)
            row["audio_url"] = f"/api/audio/{day}/{row['id']}"
        return jsonify(date=day, page=page, limit=limit, **result)

    @app.get("/api/audio/<day>/<record_id>")
    def audio(day, record_id):
        try:
            valid_day(day)
        except ValueError:
            abort(404)
        if not re.fullmatch(r"[0-9a-f]{32}", record_id):
            abort(404)
        row = store.record(day, record_id)
        if not row:
            abort(404)
        directory = store.day_dir(day) / "audio"
        if directory.is_symlink() or directory.resolve().parent != store.day_dir(day):
            abort(404)
        path = directory / row["audio_file"]
        if Path(row["audio_file"]).name != row["audio_file"] or path.resolve().parent != directory.resolve() or not path.is_file():
            abort(404)
        try:
            return send_file(path, mimetype="audio/wav", conditional=True, download_name=path.name)
        except FileNotFoundError:
            abort(404, description="音频已过期或被删除")

    @app.get("/api/export")
    def export():
        day = day_arg()
        rows = store._read(day, f"SELECT * FROM records WHERE {VISIBLE_RECORD_SQL} ORDER BY started_at, id")
        lines = [f"{day} ASR 记录（{app.config['ARCHIVE_TIMEZONE']}）\n"]
        for row in rows:
            lines.append(f"\n[{row['started_at'].replace('T', ' ')}] {row['duration']:.2f}s · {row['status']}\n")
            if row["text"]:
                for segment in json.loads(row["segments"]):
                    started = dt.datetime.fromisoformat(row["started_at"]) + dt.timedelta(seconds=segment["start"])
                    lines.append(f"[{started:%H:%M:%S}] [{segment['speaker']}] {segment['text']}\n")
        return Response("".join(lines), mimetype="text/plain", headers={"Content-Disposition": f'attachment; filename="asr_{day}.txt"'})

    return app
