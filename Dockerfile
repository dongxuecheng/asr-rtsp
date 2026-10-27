FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data TZ=Asia/Shanghai
# Use Tsinghua mirrors for Debian packages and security updates.
RUN sed -i \
        -e 's|https\?://deb\.debian\.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
        -e 's|https\?://security\.debian\.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app \
    && mkdir /data && chown app:app /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY rtsp_asr_save_audio.py rtsp_stream.py speech.py storage.py web.py ./
COPY static ./static
USER app
CMD ["python", "rtsp_asr_save_audio.py"]
