"""FFmpeg RTSP TCP transport, reconnection watchdog, and daily runtime logs."""
import datetime as dt
import os
from pathlib import Path
import queue
import re
import select
import subprocess
import sys
import threading
import time

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
print_lock = threading.Lock()
file_lock = threading.Lock()


class StreamStalledError(RuntimeError):
    pass


def redact(text):
    return re.sub(r"(rtsps?://)[^\s/]*@", r"\1***@", str(text), flags=re.IGNORECASE)


def log_print(message):
    with print_lock:
        text = redact(message)
        # Logging must never abort capture on a legacy Windows console codec.
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding), flush=True)


def ffmpeg_log_path(output_dir, event_time):
    directory = Path(output_dir) / event_time.strftime("%Y-%m-%d") / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"ffmpeg_{event_time:%Y-%m-%d}.log"


def append_text_file(path, line):
    with file_lock:
        with path.open("a", encoding="utf-8") as file:
            file.write(redact(line))


def runtime_log(output_dir, message, also_print=True):
    now = dt.datetime.now()
    try:
        directory = Path(output_dir) / now.strftime("%Y-%m-%d") / "runtime"
        directory.mkdir(parents=True, exist_ok=True)
        append_text_file(directory / f"runtime_{now:%Y-%m-%d}.log", f"[{now:%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError as exc:
        log_print(f"写 runtime 日志失败: {exc}")
    if also_print:
        log_print(message)


def drain_ffmpeg_stderr(
    process: subprocess.Popen,
    output_dir: str,
    show_ffmpeg_log: bool,
) -> None:
    """
    持续消费 FFmpeg stderr。
    不能让 stderr=PIPE 后无人读取，否则 stderr pipe 自身也可能塞满。
    """
    if process.stderr is None:
        return

    try:
        while True:
            raw_line = process.stderr.readline()

            if not raw_line:
                break

            try:
                text = raw_line.decode(
                    "utf-8",
                    errors="replace",
                ).rstrip()
            except AttributeError:
                text = str(raw_line).rstrip()

            text = redact(text)
            if not text:
                continue

            now = dt.datetime.now()
            line = (
                f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{text}\n"
            )

            try:
                append_text_file(
                    ffmpeg_log_path(output_dir, now),
                    line,
                )
            except Exception as exc:
                log_print(f"写 FFmpeg 日志失败: {exc}")

            important_error = any(
                keyword in text.lower()
                for keyword in (
                    "error",
                    "invalid",
                    "failed",
                    "unable",
                    "not found",
                    "matches no streams",
                    "option",
                    "timed out",
                    "connection refused",
                )
            )

            if show_ffmpeg_log or important_error:
                log_print(f"[FFmpeg] {text}")

    except Exception as exc:
        log_print(f"读取 FFmpeg stderr 异常: {exc}")


_ffmpeg_rtsp_timeout_option: str | None = None


def detect_ffmpeg_rtsp_timeout_option() -> str | None:
    """
    自动识别当前 FFmpeg 的 RTSP socket 超时参数。

    旧版 FFmpeg 常见:
        -stimeout <microseconds>

    新版 FFmpeg 常见:
        -timeout <microseconds>

    如果两者都没有，则不向 FFmpeg 传该参数，
    仍由 Python 层 stall watchdog 负责自愈。
    """
    global _ffmpeg_rtsp_timeout_option

    if _ffmpeg_rtsp_timeout_option is not None:
        return _ffmpeg_rtsp_timeout_option or None

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-h",
                "demuxer=rtsp",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
            check=False,
        )
        help_text = result.stdout or ""

        if re.search(r"(?m)^\s*-stimeout\b", help_text):
            _ffmpeg_rtsp_timeout_option = "stimeout"
        elif re.search(r"(?m)^\s*-timeout\b", help_text):
            _ffmpeg_rtsp_timeout_option = "timeout"
        else:
            _ffmpeg_rtsp_timeout_option = ""

    except Exception as exc:
        log_print(
            f"⚠️ 无法检测 FFmpeg RTSP timeout 参数: {exc}；"
            "将仅使用 Python watchdog"
        )
        _ffmpeg_rtsp_timeout_option = ""

    return _ffmpeg_rtsp_timeout_option or None


def start_ffmpeg(
    rtsp_url: str,
    output_dir: str,
    show_ffmpeg_log: bool,
    ffmpeg_timeout: float,
) -> tuple[subprocess.Popen, threading.Thread]:
    """
    启动 FFmpeg，并自动兼容新旧版本 RTSP timeout 参数。
    """
    timeout_us = max(1, int(ffmpeg_timeout * 1_000_000))
    timeout_option = detect_ffmpeg_rtsp_timeout_option()

    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
    ]

    if timeout_option:
        command.extend([
            f"-{timeout_option}",
            str(timeout_us),
        ])
        log_print(
            f"FFmpeg RTSP 超时参数: "
            f"-{timeout_option} {timeout_us}"
        )
    else:
        log_print(
            "⚠️ 当前 FFmpeg 未检测到 stimeout/timeout，"
            "不传 FFmpeg 超时参数，仅使用 Python watchdog"
        )

    command.extend([
        "-i",
        rtsp_url,
        "-map",
        "0:a:0",
        "-vn",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "pipe:1",
    ])

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )

    if process.stdout is None:
        stop_ffmpeg(process)
        raise RuntimeError("无法获取 FFmpeg 标准输出")

    stderr_thread = threading.Thread(
        target=drain_ffmpeg_stderr,
        args=(
            process,
            output_dir,
            show_ffmpeg_log,
        ),
        daemon=True,
        name=f"ffmpeg-stderr-{process.pid}",
    )
    stderr_thread.start()

    return process, stderr_thread


def stop_ffmpeg(process: subprocess.Popen | None) -> None:
    if process is None:
        return

    # 已退出：只做资源清理。
    if process.poll() is not None:
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass

        try:
            if process.stderr is not None:
                process.stderr.close()
        except Exception:
            pass

        return

    try:
        process.terminate()
        process.wait(timeout=3)

    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception:
            pass

        try:
            process.wait(timeout=3)
        except Exception:
            pass

    except Exception as exc:
        log_print(f"停止 FFmpeg 失败: {exc}")

    finally:
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass

        try:
            if process.stderr is not None:
                process.stderr.close()
        except Exception:
            pass


def read_exact_audio_chunk(
    process: subprocess.Popen,
    chunk_size: int,
    stall_timeout: float,
    status_interval: float,
    last_status_time: float,
    task_queue: queue.Queue,
    stop_event: threading.Event | None = None,
) -> tuple[bytes, float]:
    """
    从 FFmpeg stdout 读取一个完整 PCM chunk。

    关键点：
    - 不再直接调用阻塞式 process.stdout.read(chunk_size)。
    - Linux 下用 select 等待 pipe 可读。
    - 每隔最多 1 秒检查一次：
        1) FFmpeg 是否已经退出；
        2) 距离上一次收到音频是否超过 stall_timeout；
        3) 是否需要输出状态日志。
    """
    if process.stdout is None:
        raise RuntimeError("FFmpeg stdout 不存在")

    if os.name != "posix":
        raise RuntimeError(
            "当前 watchdog 读取方式针对 Linux/POSIX。"
            "你使用 screen 运行时应处于 Linux 环境。"
        )

    fd = process.stdout.fileno()
    raw_audio = bytearray()

    last_data_time = time.monotonic()

    while len(raw_audio) < chunk_size:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("停止读取音频")
        now = time.monotonic()

        # FFmpeg 已退出。
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"FFmpeg 已退出，returncode={return_code}"
            )

        # Python 层 watchdog。
        no_data_seconds = now - last_data_time
        if no_data_seconds >= stall_timeout:
            raise StreamStalledError(
                f"FFmpeg 连续 {no_data_seconds:.1f}s "
                f"没有输出音频，判定 RTSP/FFmpeg 卡死"
            )

        # 周期状态输出。
        if status_interval > 0 and now - last_status_time >= status_interval:
            log_print(
                "[状态] "
                f"FFmpeg PID={process.pid} 正常运行，"
                f"当前块={len(raw_audio)}/{chunk_size} bytes，"
                f"ASR队列={task_queue.qsize()}/{task_queue.maxsize}"
            )
            last_status_time = now

        # 最多等待 1 秒，让主循环能持续做 watchdog 检查。
        wait_seconds = min(
            1.0,
            max(0.05, stall_timeout - no_data_seconds),
        )

        try:
            readable, _, _ = select.select(
                [fd],
                [],
                [],
                wait_seconds,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"监听 FFmpeg stdout 失败: {exc}"
            ) from exc

        if not readable:
            continue

        remaining = chunk_size - len(raw_audio)

        try:
            # 每次最多读取 64 KiB，避免一次读取过大。
            data = os.read(
                fd,
                min(64 * 1024, remaining),
            )
        except OSError as exc:
            raise RuntimeError(
                f"读取 FFmpeg stdout 失败: {exc}"
            ) from exc

        if not data:
            return_code = process.poll()
            raise RuntimeError(
                "FFmpeg stdout EOF，"
                f"returncode={return_code}"
            )

        raw_audio.extend(data)
        last_data_time = time.monotonic()

    return bytes(raw_audio), last_status_time
