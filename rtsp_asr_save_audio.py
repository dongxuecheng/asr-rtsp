#!/usr/bin/env python3
"""RTSP speech collector: VAD -> durable WAV -> ASR -> daily SQLite records."""
import argparse
import datetime as dt
import io
import json
import math
import os
import queue
import re
import signal
import threading
import time
import wave

import requests

from rtsp_stream import (log_print, runtime_log, redact,
                         start_ffmpeg, stop_ffmpeg, read_exact_audio_chunk)
from speech import SpeechSegmenter, FRAME_BYTES, FRAME_MS
from storage import Store

DEFAULT_API_URL = "http://172.16.20.163:17003/v1/audio/transcriptions"


def create_wav_buffer(pcm):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    buffer.seek(0)
    return buffer


def extract_segments(payload):
    for obj in (payload, payload.get("data"), payload.get("result")):
        if isinstance(obj, dict) and isinstance(obj.get("segments"), list):
            segments = [s for s in obj["segments"] if isinstance(s, dict)]
            if segments:
                return segments
    return []


def extract_full_text(payload):
    for obj in (payload, payload.get("data"), payload.get("result")):
        text = obj.get("text") if isinstance(obj, dict) else obj
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def normalized_segments(payload, duration):
    result = []

    def timestamp(segment, keys, default):
        value = next((segment[k] for k in keys if k in segment), default)
        try:
            seconds = float(value)
            if not math.isfinite(seconds):
                return default
            # Preserve compatibility with the original service's ms timestamps.
            if seconds > duration * 10:
                seconds /= 1000
            return min(duration, max(0, seconds))
        except (ValueError, TypeError):
            return default

    for segment in extract_segments(payload):
        text = next((segment[k].strip() for k in ("text", "transcript", "content")
                     if isinstance(segment.get(k), str) and segment[k].strip()), "")
        if not text:
            continue
        speaker = next((str(segment[k]).strip() for k in ("speaker_id", "speaker", "speaker_label", "speaker_name")
                        if segment.get(k) is not None and str(segment[k]).strip()), "未知说话人")
        start = timestamp(segment, ("start_time", "start", "begin_time", "begin"), 0)
        end = max(start, timestamp(segment, ("end_time", "end", "finish_time", "finish"), duration))
        result.append({"start": start, "end": end, "speaker": speaker, "text": text})
    if not result:
        text = extract_full_text(payload)
        if text:
            result.append({"start": 0, "end": duration, "speaker": "未知说话人", "text": text})
    return result


def new_http_session():
    session = requests.Session()
    session.headers["User-Agent"] = "rtsp-asr-listener/3.0"
    api_key = os.environ.get("ASR_API_KEY", "")
    if api_key:
        session.headers["Authorization"] = f"Bearer {api_key}"
    return session


def worker(worker_id, task_queue, args, extra_form):
    store = Store(args.output_dir, args.retention_days)
    session = new_http_session()
    log_print(f"ASR 工作线程 {worker_id} 已启动")
    try:
        while True:
            task = task_queue.get()
            record_id = None
            try:
                if task is None:
                    return
                sequence, chunk = task
                day = chunk.started_at.date().isoformat()
                record_id = store.begin(chunk.started_at, chunk.pcm, args.prefix)
                if record_id is None:
                    continue
                form = {"model": args.model, "language": args.language, "response_format": "verbose_json",
                        "enable_speaker_diarization": "true", **extra_form}
                last_error = ""
                for attempt in range(args.retry_count + 1):
                    try:
                        with create_wav_buffer(chunk.pcm) as wav:
                            with session.post(args.api_url, files={"file": ("chunk.wav", wav, "audio/wav")},
                                              data=form, timeout=(args.connect_timeout, args.timeout)) as response:
                                if response.status_code != 200:
                                    raise RuntimeError(f"ASR HTTP {response.status_code}: {response.text[:500]}")
                                payload = response.json()
                        if not isinstance(payload, dict):
                            raise ValueError("API JSON 顶层必须是对象")
                        segments = normalized_segments(payload, chunk.duration)
                        break
                    except (requests.RequestException, ValueError, RuntimeError) as exc:
                        last_error = redact(f"{type(exc).__name__}: {exc}")
                        session.close()
                        session = new_http_session()
                        if attempt < args.retry_count:
                            time.sleep(min(2 ** attempt, 5))
                else:
                    store.finish(day, record_id, error=last_error)
                    runtime_log(args.output_dir, f"[任务 {sequence}] 识别失败，音频已保留: {last_error}")
                    continue
                # Storage failures must not trigger duplicate ASR requests.
                store.finish(day, record_id, segments=segments, payload=payload if args.save_json else None)
                log_print(f"[任务 {sequence}] 保存 {chunk.duration:.2f}s 音频，识别 {len(segments)} 个片段")
            except Exception as exc:
                runtime_log(args.output_dir, f"ASR 工作线程 {worker_id} 异常: {type(exc).__name__}: {exc}")
                if record_id:
                    try:
                        store.finish(day, record_id, error=redact(str(exc)))
                    except Exception as storage_exc:
                        log_print(f"更新失败状态异常: {storage_exc}")
            finally:
                task_queue.task_done()
    finally:
        session.close()


def put_task(task_queue, task):
    try:
        task_queue.put_nowait(task)
        return
    except queue.Full:
        log_print("⚠️ ASR 队列已满，丢弃最旧的待处理语音；请增加 workers 或提升 ASR 速度")
    try:
        task_queue.get_nowait()
        task_queue.task_done()
    except queue.Empty:
        pass
    task_queue.put_nowait(task)


def cleanup_loop(store, interval, stop_event):
    while not stop_event.is_set():
        try:
            removed = store.cleanup()
            if removed:
                log_print(f"[定时清理] 已删除日期目录: {', '.join(removed)}")
        except Exception as exc:
            log_print(f"[定时清理] 失败，将在下轮重试: {exc}")
        stop_event.wait(interval)


def listen_rtsp(args, extra_form, stop_event=None):
    if os.name != "posix":
        raise RuntimeError("采集器需运行在 Linux/POSIX；Windows 请使用 Docker Compose")
    stop_event = stop_event or threading.Event()
    # Fail before starting background threads if the VAD installation is invalid.
    def make_segmenter():
        return SpeechSegmenter(max_seconds=args.chunk, mode=args.vad_mode, threshold=args.threshold,
                               min_speech_ms=args.min_speech_ms, silence_ms=args.silence_ms, padding_ms=args.padding_ms)
    make_segmenter()
    store = Store(args.output_dir, args.retention_days)
    store.cleanup()
    store.recover_pending()
    task_queue = queue.Queue(maxsize=args.queue_size)
    threads = [threading.Thread(target=worker, args=(i+1, task_queue, args, extra_form),
                                name=f"asr-worker-{i+1}", daemon=True) for i in range(args.workers)]
    for thread in threads:
        thread.start()
    cleaner = threading.Thread(target=cleanup_loop, args=(store, args.cleanup_interval, stop_event), daemon=True)
    cleaner.start()
    sequence = 0
    last_status = time.monotonic()
    process = None
    runtime_log(args.output_dir, f"采集启动：VAD={args.vad_mode}，最长语音块={args.chunk}s，保留={args.retention_days}天")

    def submit(chunks):
        nonlocal sequence
        for chunk in chunks:
            put_task(task_queue, (sequence, chunk))
            sequence += 1

    try:
        while not stop_event.is_set():
            segmenter = make_segmenter()
            stderr_thread = None
            try:
                log_print(f"正在连接 RTSP: {args.rtsp_url}")
                process, stderr_thread = start_ffmpeg(args.rtsp_url, args.output_dir, args.show_ffmpeg_log, args.ffmpeg_timeout)
                origin = None
                frame_index = 0
                while not stop_event.is_set():
                    pcm, last_status = read_exact_audio_chunk(process, FRAME_BYTES, args.stall_timeout,
                                                              args.status_interval, last_status, task_queue, stop_event)
                    if origin is None:
                        origin = dt.datetime.now() - dt.timedelta(milliseconds=FRAME_MS)
                        runtime_log(args.output_dir, "RTSP 已成功读取到音频")
                    # Sample-based time keeps the original timeline including silence.
                    started = origin + dt.timedelta(milliseconds=frame_index * FRAME_MS)
                    frame_index += 1
                    submit(segmenter.feed(pcm, started))
            except InterruptedError:
                if not stop_event.is_set():
                    raise
            except FileNotFoundError as exc:
                raise RuntimeError("没有找到 ffmpeg，请先安装或使用 Docker 镜像") from exc
            except Exception as exc:
                runtime_log(args.output_dir, f"RTSP 监听异常，{args.reconnect_delay}s 后重连: {type(exc).__name__}: {exc}")
            finally:
                submit(segmenter.flush())
                stop_ffmpeg(process)
                process = None
                if stderr_thread:
                    stderr_thread.join(timeout=1)
            stop_event.wait(args.reconnect_delay)
    finally:
        stop_event.set()
        stop_ffmpeg(process)
        log_print("停止采集，等待已排队的 ASR 任务完成（受 HTTP 超时约束）...")
        for _ in threads:
            task_queue.put(None)
        for thread in threads:
            thread.join()
        cleaner.join(timeout=2)
        runtime_log(args.output_dir, "采集程序已安全退出")


def parse_extra_form(value):
    data = json.loads(value) if value.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("ASR_EXTRA_FORM / --extra-form 必须是 JSON 对象")
    return {str(key): str(item).lower() if isinstance(item, bool) else str(item) for key, item in data.items()}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="RTSP 语音检测、ASR 转写、WAV 归档和定时清理")
    parser.add_argument("rtsp_url", nargs="?", default=os.getenv("RTSP_URL"), help="也可设置 RTSP_URL 环境变量")
    options = [
        ("api-url", "ASR_API_URL", str, DEFAULT_API_URL), ("model", "ASR_MODEL", str, "qwen3-asr-0.6b"),
        ("language", "ASR_LANGUAGE", str, "zh"), ("output-dir", "DATA_DIR", str, "./data"),
        ("prefix", "AUDIO_PREFIX", str, "meeting"), ("extra-form", "ASR_EXTRA_FORM", str, ""),
        ("chunk", "MAX_SEGMENT_SECONDS", float, 45), ("workers", "ASR_WORKERS", int, 1),
        ("queue-size", "QUEUE_SIZE", int, 10), ("threshold", "RMS_THRESHOLD", int, 20),
        ("timeout", "ASR_TIMEOUT", float, 180), ("connect-timeout", "ASR_CONNECT_TIMEOUT", float, 10),
        ("retry-count", "ASR_RETRY_COUNT", int, 2), ("reconnect-delay", "RECONNECT_DELAY", float, 5),
        ("ffmpeg-timeout", "FFMPEG_TIMEOUT", float, 20), ("stall-timeout", "STALL_TIMEOUT", float, 30),
        ("status-interval", "STATUS_INTERVAL", float, 60), ("vad-mode", "VAD_MODE", int, 2),
        ("min-speech-ms", "MIN_SPEECH_MS", int, 300), ("silence-ms", "SILENCE_MS", int, 600),
        ("padding-ms", "PADDING_MS", int, 300), ("retention-days", "RETENTION_DAYS", int, 7),
        ("cleanup-interval", "CLEANUP_INTERVAL_SECONDS", float, 3600),
    ]
    for flag, env, kind, default in options:
        parser.add_argument(f"--{flag}", type=kind, default=os.getenv(env, str(default)))
    parser.add_argument("--save-json", action="store_true", default=os.getenv("SAVE_RAW_JSON", "false").lower() in ("1", "true", "yes"))
    parser.add_argument("--show-ffmpeg-log", action="store_true", default=os.getenv("SHOW_FFMPEG_LOG", "false").lower() in ("1", "true", "yes"))
    args = parser.parse_args(argv)
    if not args.rtsp_url or not args.rtsp_url.lower().startswith(("rtsp://", "rtsps://")):
        parser.error("请设置有效的 RTSP_URL 或传入 rtsp:// 地址")
    if not args.api_url.startswith(("http://", "https://")):
        parser.error("ASR_API_URL 必须为 http(s) 地址")
    for name in ("chunk", "workers", "queue_size", "timeout", "connect_timeout", "ffmpeg_timeout", "stall_timeout",
                 "min_speech_ms", "silence_ms", "padding_ms", "cleanup_interval"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{name} 必须是大于 0 的有限数值")
    for name in ("threshold", "retry_count", "reconnect_delay", "status_interval", "retention_days"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0:
            parser.error(f"{name} 必须是非负有限数值")
    if args.vad_mode not in range(4):
        parser.error("VAD_MODE 必须为 0、1、2 或 3")
    if args.chunk * 1000 < args.padding_ms + args.min_speech_ms or args.chunk * 1000 < args.silence_ms:
        parser.error("最长片段应至少容纳 padding + min_speech，且不小于 silence")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.prefix):
        parser.error("AUDIO_PREFIX 仅允许 1～64 位字母、数字、下划线和连字符")
    try:
        parse_extra_form(args.extra_form)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    return args


def main():
    args = parse_args()
    stop_event = threading.Event()
    def stop(signum, frame):
        stop_event.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    listen_rtsp(args, parse_extra_form(args.extra_form), stop_event)


if __name__ == "__main__":
    main()
