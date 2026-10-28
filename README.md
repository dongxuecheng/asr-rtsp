# RTSP 语音归档

持续读取一路 RTSP 音频，检测人声并调用已有 ASR 服务，**仅在识别成功且有非空文本时保存 WAV 与文本**。项目 `data/` 目录下自动生成每日 UTF-8 TXT 转写日志，可直接用文本编辑器查看。网页默认完整展示当天的 ASR 文字，每 2 秒自动检查新结果，提供说话人、时间、日期查询、关键词搜索和当日 TXT 导出，无分页及音频播放控件。支持断流重连、无数据 watchdog、HTTP 重试、容器停止时排空队列，以及按日期定时删除历史数据。

## 原脚本的问题与改造

原脚本已有 FFmpeg TCP 拉流、`select` 超时检测、ASR 工作队列和异常重试，适合长期监听。但原来的 `--threshold` 是对 **整个 45 秒块** 计算 RMS；一小段讲话也会使整块被保存，背景噪声也可能通过过滤。原脚本在 ASR 请求前保存 WAV，因此识别失败和没有文字的块也会保留，但没有可查询的状态索引、前端和保留期限。它还依赖已从 Python 3.13 移除的 `audioop`。

现在保留拉流与重试能力，将代码拆分为：

| 文件 | 职责 |
| --- | --- |
| `rtsp_asr_save_audio.py` | 配置、采集循环、ASR worker、清理调度、SIGTERM 退出 |
| `rtsp_stream.py` | FFmpeg、stderr 消费、watchdog、按日运行日志、RTSP 密码脱敏 |
| `speech.py` | 30ms WebRTC VAD、人声切段、样本时间轴、无 audioop 的 RMS |
| `storage.py` | 每日 SQLite / TXT、有效转写与 WAV 归档、历史无效录音与过期目录清理 |
| `web.py`、`static/` | Flask 只读 API、自动刷新的全天转写文字页面 |
| `Dockerfile`、`compose.yaml` | 采集与网页服务、项目 data/ 目录挂载、目录权限初始化、日志轮转 |

数据流：`RTSP → FFmpeg 16kHz 单声道 PCM → VAD → ASR 队列 → 内存中请求 ASR → 成功且有文本 → WAV + SQLite + 每日 TXT`。识别失败或无文本的片段直接丢弃，不写入录音、原始 JSON、转写 TXT 或记录。

页面预览（演示数据）：

![每日语音档案](docs/preview.png)

## 启动

需要运行中的 Docker Engine / Docker Desktop，支持 Compose v2。ASR **继续使用已有的 HTTP 服务**，镜像不包含 ASR 模型，也不需要 GPU。

Docker 构建时 APT 使用[清华 TUNA Debian 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/debian/)及其 Debian Security 镜像安装系统依赖；pip 使用[清华 TUNA PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)安装 Python 依赖。

1. 在项目目录复制配置：

   ```powershell
   Copy-Item .env.example .env
   ```

   Linux：`cp .env.example .env`。

2. 编辑 `.env`，至少修改 `RTSP_URL`，核对 `ASR_API_URL`。容器必须能访问摄像头和 ASR 所在网络。Docker Desktop 内访问宿主机 ASR 可使用 `http://host.docker.internal:端口/v1/audio/transcriptions`；容器内的 `localhost` 指容器自身。

3. 构建并启动：

   ```sh
   docker compose up -d --build
   docker compose logs -f collector
   ```

4. 打开 [实时转写页面](http://localhost:8080)。没有有效转写时页面显示等待状态。网页和采集服务独立运行，摄像头断流时仍可查看历史记录。

默认仅宿主机可访问网页。如需局域网访问，将 `WEB_BIND=0.0.0.0`，同时设置 `WEB_USERNAME` 和 `WEB_PASSWORD`；页面、API、录音和静态文件一起受 HTTP Basic 认证保护。跨公网使用时通过 HTTPS 反向代理访问。

`.env` 的值如含 `$`、`#`，请使用单引号；RTSP 用户名/密码中的 `@`、`:` 等保留字符还需要 URL 编码。`.env` 不会打包进镜像。

修改 `.env` 后运行 `docker compose up -d`，会重建配置发生变化的容器；仅执行 `restart` 不会载入新的环境变量。

## 网页实时转写

默认选择采集端时区的今天，按录音时间完整展示当天全部有效 ASR 文本，超过 50 条也不分页。每条记录显示时间、说话人和转写文字，页面没有音频播放器或录音下载控件；WAV 仍按原规则保存在项目 `data/` 目录。

页面每 2 秒检查一次更新，**ASR 结果保存后通常在下一轮检查时显示**，时间还受网络影响。采集分段和 ASR 推理本身仍需要时间，这不是逐字流式识别。没有变化时接口返回 HTTP 304，不重复传输当天全部文字；有新内容时更新已有列表，保持阅读位置，不整页刷新。

启用“跟随最新”且在列表底部时，新转写会自动滚动到最新位置；阅读较早内容时保留当前位置，并提示新增条数，点击“查看最新”即可跳到列表底部。取消勾选后仅更新文字，不自动滚动。切换日期可阅读历史，点击“今天”恢复跟随当天，跨午夜时会自动切换日期。搜索仅筛选当前日期的文本，搜索结果也会自动更新。

连接暂时中断时保留已显示的文字并自动重试；浏览器标签页隐藏时暂停轮询，返回页面会立即同步。

## “只保存有效声音”的具体含义

当前实现针对 **人声**，使用 WebRTC VAD，而不是把所有非零声波都保存。它按 30ms 帧检测，默认满足一段持续人声后启动，带最多约 300ms 前缓冲，遇约 600ms 连续非语音结束。短暂停顿保留，较长静音不落盘，也不送 ASR；连续长语音最多每 45 秒拆段。停止或断流时会提交尚未结束的有效片段。

音频按连续片段分别保存，不把不同时间的讲话拼成一条；记录包含采集起始时间，ASR 的片段偏移相对于各自 WAV，网页将其换算为说话时间显示。跨午夜的片段按**片段开始日期**归档。时间以采集器收到第一帧时的本地时钟为锚点，按样本数推进，并非摄像头原始 PTS。

VAD 是检测器，会有漏检和误检：远处很小的说话声可能漏掉，音乐或某些噪声可能被判为人声。通过 VAD 后还需经过 ASR：**仅保留识别成功且去除首尾空白后有文本的录音**。请求失败、非法响应、空文本和纯空白文本均不归档，网页的日期列表、统计、搜索、音频接口和导出也只包含成功且有文本的记录。识别中的片段在内存中处理，不会出现在页面中。

这里不对文本进行语义判断：标点、误识别文字等非空内容仍会保留；一段录音只要有有效转写，就保存其完整 VAD 片段。

| 配置项 | 默认 | 调整建议 |
| --- | --- | --- |
| `VAD_MODE` | `2` | `0` 最宽松，`3` 最严格；噪声误检多时增大，漏掉轻声时减小 |
| `RMS_THRESHOLD` | `20` | 16-bit PCM 帧音量下限；`0` 关闭 RMS 门限，仍启用 VAD |
| `MIN_SPEECH_MS` | `300` | 每个独立片段最少累计人声；很短的应答可能需要调低 |
| `SILENCE_MS` | `600` | 连续非语音多久结束；增大可保留更长的句间停顿 |
| `PADDING_MS` | `300` | 开始检测的人声窗口及前缓冲；通常保持默认 |
| `MAX_SEGMENT_SECONDS` | `45` | 连续语音的最大片段，避免请求和内存无上限增长 |

WAV 为 16kHz / 单声道 / 16-bit，约 **115MB/小时保存音频**，实际占用取决于讲话时长；如果一天仅有两小时有效讲话，约 230MB/天，加少量数据库和日志。

VAD 输入格式依据 [webrtcvad-wheels 文档](https://pypi.org/project/webrtcvad-wheels/)。音频文件及兼容的下载接口保留，网页阅读界面不加载音频。

## 定时删除

```dotenv
RETENTION_DAYS=7
CLEANUP_INTERVAL_SECONDS=3600
```

默认保留**最近 7 个本地日历日期，含今天**，每小时检查一次，采集器启动时也检查。举例：2026-10-26 保留 10-20 至 10-26，删除 10-19 及之前的日期目录。它不是逐条精确到 168 小时；跨午夜片段的尾部会随其开始日期一起清理。

清理会一并删除该日期的 SQLite（含 ASR 文本、说话人和错误状态）、转写 TXT、WAV、可选原始 JSON 和运行日志。仅处理数据根目录下合法的 `YYYY-MM-DD` 子目录，不跟随日期目录符号链接。`RETENTION_DAYS=0` 关闭自动删除。缩短保留期限后，采集器重新启动即会清理到期数据。

清理调度在 `collector` 中运行，**collector 停止期间不会清理**，下次启动时补清；每次删除失败会在日志报告，下轮重试。Docker 标准输出日志另行限制为每服务最多 3 份 × 10MB。

升级后，collector 启动时会额外清理旧版本遗留的 `failed`、`empty`、`pending` 及无文本成功记录，同时删除对应 WAV 和原始 JSON；之后每轮清理也会重试。此规则独立于过期保留期限，即使 `RETENTION_DAYS=0`，无效历史录音也会被清理。网页会立即隐藏这些旧记录，无需等待文件清理完成。物理文件删除失败时保留隐藏记录以便重试。

## 数据位置与备份

默认直接保存到**项目目录下的 `data/`**（与 `compose.yaml` 同级）。采集器把这里挂载到容器内的 `/data`，网页只读挂载同一个目录。在 Windows 项目路径 `D:\code\asr-rtsp` 中部署时，实际文件位于 `D:\code\asr-rtsp\data`；在 Linux 服务器上则位于该服务器的项目目录中。

文件结构：

```text
项目目录/data/
  2026-10-28/
    asr_2026-10-28.txt                   # 可直接阅读的每日转写日志，UTF-8
    records.sqlite3                     # ASR 文本、说话人、时间及音频文件名
    audio/meeting_09-30-00-000000_<UUID>.wav
    raw_json/<UUID>.json                 # SAVE_RAW_JSON=true 且成功有文本才写入
    runtime/runtime_2026-10-28.log
    runtime/ffmpeg_2026-10-28.log
```

音频是独立 WAV 文件，ASR 内容同时保存在每日 TXT 和 SQLite 中：TXT 便于直接阅读，SQLite 继续用于网页搜索与索引。`data/` 自动创建，`data-init` 初始化服务负责把目录及已有文件的所有者设置为容器 UID/GID `10001:10001`，之后采集和网页服务仍以普通用户运行；Linux 服务器上无需另行手工 chown。目录中的文件可在宿主机查看、复制，停止或重建容器不会删除它们，过期清理仍按 `.env` 执行。

每条成功转写完成后即时追加至 `data/日期/asr_日期.txt`，无需额外配置。内容包含录音起始时间、时长、WAV 相对路径，以及每个说话片段的实际起止时间、说话人和文本。例如：

```text
2026-10-28 ASR 记录（Asia/Shanghai）

[2026-10-28 09:30:00.000] 时长 3.00s
音频: audio/meeting_09-30-00-000000_<UUID>.wav
[2026-10-28 09:30:00.100 - 2026-10-28 09:30:02.800] [speaker_0] 今天开始会议。
```

多个 ASR worker 按完成顺序追加；采集器启动和每轮定时清理时，会从数据库按录音时间重新整理 TXT，补齐已有历史转写、修复丢失或不完整的 TXT，不重复写入记录。TXT 写入失败不会删除已经归档的数据库和音频，之后会重试同步。TXT 是自动生成的日志，手工修改会在同步时被数据库内容覆盖；网页“导出当日文本”使用相同格式并按录音时间排序。

`data/` 已被 Git 和 Docker 构建忽略，不会随代码上传 GitHub 或打包进镜像。容器的 `DATA_DIR=/data` 是挂载路径，不需要在 `.env` 中修改它。首次启动但尚无成功转写时，可能还没有日期数据库或 WAV 文件。

备份时先停止采集，再复制项目中的整个 `data/` 文件夹，避免复制到正在写入的数据库：

```sh
docker compose stop collector
# 将项目的 data/ 文件夹复制到备份位置
docker compose start collector
```

从旧版命名卷升级且希望保留历史数据时，**在重建旧容器之前**执行下面步骤；`data/` 应为空目录或尚不存在：

```sh
docker compose stop collector web
docker compose cp collector:/data/. ./data
git pull
docker compose up -d --build
```

复制命令从旧采集容器的 `/data` 读取历史文件，不会删除旧卷；升级不会自动迁移已有命名卷。如果已经重建了容器，旧卷 `rtsp-asr_asr_data` 仍在，需另行从旧卷恢复数据。首次安装或不需要旧数据时，直接执行 `docker compose up -d --build` 即可。

只运行一个 collector 写入同一数据目录；多个 RTSP 源应使用不同项目目录或独立的 data/ 挂载路径，单独修改 Compose 项目名不会改变 `./data` 的位置。

## ASR 与运行配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `ASR_API_URL` | 原脚本地址 | OpenAI 风格 `/v1/audio/transcriptions` 表单接口 |
| `ASR_MODEL` / `ASR_LANGUAGE` | `qwen3-asr-0.6b` / `zh` | 模型名、语言 |
| `ASR_API_KEY` | 空 | 可选 Bearer Token |
| `ASR_EXTRA_FORM` | `{}` | JSON 对象，覆盖或增加表单字段；JSON 布尔值会转换为小写 true/false |
| `ASR_WORKERS` / `QUEUE_SIZE` | `1` / `10` | 请求并发和内存队列上限 |
| `ASR_CONNECT_TIMEOUT` / `ASR_TIMEOUT` | `10` / `180` | 连接、读取超时，秒 |
| `ASR_RETRY_COUNT` | `2` | 首次失败后最多重试次数；网络失败会重建 Session |
| `SAVE_RAW_JSON` | `false` | 是否额外保存有效转写对应的完整接口响应 |
| `FFMPEG_TIMEOUT` / `STALL_TIMEOUT` | `20` / `30` | FFmpeg TCP 超时、Python 无 PCM watchdog，秒 |
| `RECONNECT_DELAY` | `5` | 断流重连间隔，秒 |
| `STATUS_INTERVAL` | `60` | 拉流状态日志间隔，`0` 关闭 |
| `SHOW_FFMPEG_LOG` | `false` | 是否把全部 FFmpeg 日志也输出到终端，文件始终保留 |
| `TZ` | `Asia/Shanghai` | 两个服务应使用相同时区 |

默认请求 `verbose_json` 且 `enable_speaker_diarization=true`，需服务端支持；可用 `ASR_EXTRA_FORM` 覆盖。支持顶层或 `data` / `result` 中的 `text`、`segments`，空 segments 会回退到完整文本。说话人编号来自每次 ASR 返回，**不保证跨请求是同一个人**。时间戳保留原服务的秒/毫秒启发式兼容逻辑，默认按秒，明显超出片段时长的数值按毫秒解析。

队列满时延续原脚本行为，丢弃最旧的**尚未进入 worker** 的片段并记录警告；这些片段没有保存。ASR 过慢时应提升服务速度或并发；本方案不保证拥塞、断流或强制退出时零丢失。SIGTERM 后会等待队列处理完，Compose 最多等待 2 分钟；如果已有大量慢请求，需要调大 `stop_grace_period`。ASR 处理中尚未归档的音频仅在内存中，强制退出后无法恢复；失败请求在配置的重试次数耗尽后直接丢弃，不保存历史失败项。

## 本地开发与验证

推荐 Python 3.12。Linux 安装 FFmpeg、依赖后，也可以直接运行：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python rtsp_asr_save_audio.py 'rtsp://user:password@camera/stream' --output-dir ./data
```

另一个终端：

```sh
DATA_DIR=./data gunicorn --bind 127.0.0.1:8080 'web:create_app()'
```

直接运行 Python 不会自动读取 `.env`，需要在 shell 设置环境变量或使用脚本的 CLI 参数。Windows 可以开发网页和运行测试，采集器的 `select(pipe)` 依赖 POSIX，应使用 Linux 容器。临时启动本地网页可用 `python -m flask --app 'web:create_app()' run --port 8080`。

```sh
python -X utf8 -m unittest discover -s tests -v
docker compose --env-file .env.example config --no-env-resolution --quiet
```

测试覆盖静音与短脉冲过滤、实际 VAD 的静音检测、长语音拆分与尾段、ASR 有文本后才落盘、失败/空白响应不保存、TXT 内容与并发追加、历史 TXT 补齐和修复、历史无效录音清理与删除重试、超过 50 条的全天查询、条件刷新和新增/删除检测、搜索、兼容分页与音频接口、导出、跨日归档、清理边界和认证。真实摄像头与 ASR 的识别效果、网络连通性仍需填写实际配置后验证。

旧脚本的 `transcription_logs` 不会自动迁移到网页索引。项目内已有 SQLite 归档会在升级启动时自动补生成每日 TXT，无需从网页手工导出；旧运行日志目录如迁入新数据根目录，也会受日期清理策略影响。
