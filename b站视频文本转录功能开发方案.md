# B站视频文本转录工具 · 功能开发方案

> 版本：v1.0　日期：2026-10-29　基准文档：《b站视频文本转录技术方案.md》

## 0. 约定

- **任务状态机**：`queued → parsing → subtitle → downloading → transcribing → exporting → done`，失败为 `failed`，取消为 `cancelled`
- **统一结果结构**（字幕和识别都输出这个格式，导出模块只认它）：

```json
{
  "source": "subtitle_cc | subtitle_ai | asr_local | asr_remote",
  "model": "paraformer-zh",
  "duration_ms": 3600000,
  "sentences": [
    {"start": 1230, "end": 4560, "text": "大家好，今天聊聊……", "spk": 0}
  ]
}
```

- 时间单位统一用毫秒；`spk` 只在开了说话人分离时才有
- 下表中“里程碑”对应技术方案第 8 节：M1 本地完整版，M2 识别后端抽象 + worker，M3 远程接入

---

## 1. 链接解析（M1）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 链接解析 | 识别输入格式 | `bili.py::normalize(text)`：用正则从任意文本中提取 `BV[0-9A-Za-z]{10}`、`av\d+`、`b23.tv/xxx`、`bilibili.com/video/...?p=N`；也支持直接粘贴分享文案（标题加链接混在一起的文本） |
| 链接解析 | 短链展开 | 遇到 `b23.tv` 用 `httpx`（`follow_redirects=True`，设 5s 超时）拿到最终 URL，再规范化为 `https://www.bilibili.com/video/BVxxx` |
| 链接解析 | 获取视频信息 | `yt_dlp.YoutubeDL({"skip_download": True, "cookiefile": ...}).extract_info(url, download=False)`，取 `title`、`uploader`、`duration`、`thumbnail`；放到线程池里执行，不阻塞事件循环 |
| 链接解析 | 分P列表 | 返回结果是 playlist（有 `entries`）时，把每个分P整理成 `{index, title, duration, url}`；单P视频也包装成长度为 1 的列表，前端统一处理 |
| 链接解析 | 字幕可用性预判 | 从每个分P的 `subtitles` 字段里排除 `danmaku`，标出有哪几种字幕（CC 中文 / `ai-zh` 等），前端显示“有字幕”标签 |
| 链接解析 | 解析缓存 | 按 BV 号在内存里缓存 10 分钟（LRU），避免重复解析；Cookie 改了就清空缓存 |
| 链接解析 | 接口 | `POST /api/parse {text}` → `{bvid, title, uploader, cover, parts:[...]}`；出错返回统一错误码（见第 12 节） |

## 2. 字幕获取（M1）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 字幕获取 | 字幕优先策略 | 任务没开“强制转录”时，按 **人工 CC 中文 → AI 中文（`ai-zh`）→ 其他中文** 的顺序挑选；都没有就进入音频下载 |
| 字幕获取 | 下载字幕 | 从 `subtitles[lang]` 里取下载地址，用 `httpx` 拉取；B 站原始字幕是 JSON（`body[].from/to/content`），用 yt-dlp 转出来的则可能是 srt，两种都写解析器 |
| 字幕获取 | 转为统一结构 | JSON：`from/to` 是秒 → ×1000；SRT：用正则解析时间轴；去掉空行和重复行，合并到 `sentences` |
| 字幕获取 | 登录依赖提示 | AI 字幕通常要登录才能拿到：没配 Cookie 又拿不到字幕时，在任务日志里提示“配置 Cookie 后可能能拿到 AI 字幕”，然后继续走转录流程 |
| 字幕获取 | 标记来源 | 结果的 `source` 记为 `subtitle_cc` 或 `subtitle_ai`，前端显示来源标签；用户可以对同一分P点“重新转录”（强制走 ASR） |

## 3. 音频下载与预处理（M1）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 音频下载 | 只下载音频 | yt-dlp 参数：`format="bestaudio/best"`、`outtmpl="data/cache/{bvid}_p{n}.%(ext)s"`、`cookiefile`；B 站 DASH 的音频流是 m4a，不下载视频 |
| 音频下载 | 下载进度 | 用 `progress_hooks` 回调，按 `downloaded_bytes / total_bytes` 算出 0–100%，推给任务进度（见第 6 节） |
| 音频下载 | 取消下载 | 任务被取消时，在 hook 里抛出 `DownloadCancelled`，yt-dlp 会中止下载，然后清理 `.part` 文件 |
| 音频下载 | 重试 | 配置 `retries=3`、`fragment_retries=3`；遇到 412/风控时提示配置 Cookie |
| 音频预处理 | 转成识别格式 | `ffmpeg -y -i in.m4a -ac 1 -ar 16000 -vn out.wav`，用 `subprocess.run` 调用，检查返回码，失败时把 stderr 最后 20 行写进日志 |
| 音频预处理 | 远程上传格式 | remote 后端额外转一份 `-c:a libopus -b:a 32k out.ogg`（1 小时约 15–30 MB），减少上传时间 |
| 音频预处理 | 时长与校验 | 用 `ffprobe` 读时长，写入 `duration_ms`，用于预估识别进度；时长为 0 或超过上限（默认 6 小时）就报错 |
| 音频缓存 | 复用与清理 | 缓存键 `{bvid}_p{n}`：已有 wav 就跳过下载；设置页提供“清理音频缓存”，也可配置“任务完成后自动删除音频”（默认开启） |

## 4. 本地识别 LocalASR（M1）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 本地识别 | 模型加载 | `asr/local.py`：懒加载单例 `AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc", device="cpu")`；第一次加载时页面显示“正在下载/加载模型（约 1 GB）” |
| 本地识别 | 说话人分离 | 开了说话人分离时，另外加载一个带 `spk_model="cam++"` 的实例（两个实例分开缓存，不重复加载） |
| 本地识别 | 识别调用 | `model.generate(input=wav, batch_size_s=300, hotword="词1 词2", sentence_timestamp=True)`；取 `res[0]["sentence_info"]` 里的 `start/end/text/spk`，转成统一结构 |
| 本地识别 | 热词 | 前端传数组，后端去重后用空格拼接；单个热词最长 10 个字，最多 100 个，超出就截断并提示 |
| 本地识别 | 进度预估 | FunASR 没有进度回调：按 `duration_ms × RTF`（CPU 初始 0.1，每次实测后用滑动平均更新并保存到设置）算预计耗时，进度最多涨到 95%，完成时跳到 100% |
| 本地识别 | 兜底 | `sentence_info` 为空时，用 `res[0]["text"]` 生成一条没有时间戳的句子，并标记 `no_timestamp`，导出 srt 时给出提示 |
| 本地识别 | 资源控制 | 用 `torch.set_num_threads` 设成 CPU 核数减 1，保证机器还能正常用 |

## 5. 识别后端抽象与远程识别（M2 / M3）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 后端抽象 | 统一接口 | `asr/base.py`：`class ASRBackend: def transcribe(audio_path, opts, progress_cb, cancel_event) -> Result`；`opts = {hotwords, spk, model}` |
| 后端抽象 | 选择策略 | `asr/__init__.py::get_backend(settings, task)`：任务里指定了就用任务的，否则用设置里的默认值；remote 选了 Nano 模型时，local 不支持，就提示改用 paraformer |
| 远程识别 | RemoteASR 调用 | `asr/remote.py`：用 `httpx` 发 multipart 请求 `POST {base}/transcribe`（字段：file=ogg、hotwords、spk、model），带 `Authorization: Bearer <token>`；连接超时 5s，读取超时按音频时长动态算（时长/10，最少 120s） |
| 远程识别 | 进度 | 上传阶段按已发送字节数算进度（0–20%）；识别阶段按 GPU 的 RTF（初始 0.01）预估，最多到 95% |
| 远程识别 | 自动退回本地 | 连不上、超时或 5xx 时，在任务日志里写“远程不可用，已切换本地”，改用 LocalASR 重跑；401 不退回，直接报错“Token 错误” |
| 远程识别 | 健康检查 | `GET /api/remote/health` 由本地代理调用 worker 的 `/health`，返回 `{online, gpu, models, latency_ms}`；前端每 30 秒轮询一次，更新顶部状态灯 |

## 6. 任务队列与进度推送（M1）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 任务队列 | 创建任务 | `POST /api/tasks {bvid, parts:[n...], force_asr, hotwords, spk, backend, model}`：每个分P建一条任务记录（多选分P就批量建），然后放入 `queue.Queue` |
| 任务队列 | 工作线程 | `tasks.py`：服务启动时开一个守护线程，循环 `get()` 取任务，按状态机依次调用 解析 → 字幕 → 下载 → 识别 → 导出；每切换一个阶段都写入 SQLite |
| 任务队列 | 取消 | `POST /api/tasks/{id}/cancel`：排队中的直接标记 `cancelled`；运行中的设置 `cancel_event`，下载和阶段切换时检查它；识别中途没法打断，等这一段跑完再取消 |
| 任务队列 | 重试 | `POST /api/tasks/{id}/retry`：复制参数建一个新任务，复用已缓存的音频 |
| 任务队列 | 重启恢复 | 启动时把状态不是 `done/failed/cancelled` 的任务改为 `failed(reason=服务重启)`，前端显示“重试”按钮 |
| 进度推送 | 线程到 SSE 的桥接 | 维护一个 `EventBus`，每个任务对应一组 `asyncio.Queue` 订阅者；工作线程通过 `loop.call_soon_threadsafe(q.put_nowait, event)` 推送 |
| 进度推送 | SSE 接口 | `GET /api/tasks/{id}/events`（sse-starlette）：一连上先推一次当前快照，之后推 `{stage, percent, message}`；完成或失败后推 `end` 事件并关闭；每 15 秒发一次心跳 |
| 进度推送 | 全局列表刷新 | `GET /api/events`：推送所有任务的状态变化，让任务列表页不用逐个订阅 |

## 7. 结果导出（M1）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 导出 | txt | 句子按段落合并：两句间隔超过 2 秒或者换了说话人就分段，段落之间空一行；开头写标题、链接、来源 |
| 导出 | srt | 序号 + `HH:MM:SS,mmm --> HH:MM:SS,mmm` + 文本；单条超过 40 个字按标点拆开，时间按字数比例分配 |
| 导出 | md | 一级标题写视频名，元信息用引用块（链接、UP 主、时长、来源、模型）；正文每段开头写 `[mm:ss]`，链接到 `https://www.bilibili.com/video/BVxxx?p=n&t=秒`；有说话人时加 `**说话人1**：` |
| 导出 | 下载接口 | `GET /api/tasks/{id}/export?fmt=txt|srt|md`：带 `Content-Disposition` 下载头，文件名 `{标题}_P{n}.{ext}`，非法字符要过滤 |
| 导出 | 批量导出 | `POST /api/export/zip {task_ids, fmt}`：用 `zipfile` 在内存里打包后返回，适合多P合集 |
| 导出 | 落盘 | 任务完成时把三种格式都写到 `data/results/{task_id}/`，方便在资源管理器里直接打开 |

## 8. 存储与历史（M1）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 存储 | 表结构 | `tasks(id, bvid, part, title, uploader, duration_ms, status, stage, percent, source, backend, model, opts_json, error, created_at, finished_at)`；`results(task_id PK, json)`；`settings(key PK, value_json)` |
| 存储 | 访问层 | `db.py`：标准库 `sqlite3`，开 `check_same_thread=False` 并加锁；开启 WAL 模式；启动时 `CREATE TABLE IF NOT EXISTS`，再加一个简单的 schema_version 做迁移 |
| 历史 | 列表与搜索 | `GET /api/tasks?q=&status=&page=`：按标题或 BV 号 `LIKE` 搜索，分页每页 20 条，按创建时间倒序 |
| 历史 | 全文搜索 | v1 只搜标题；以后加 SQLite FTS5 搜转录正文（先预留） |
| 历史 | 删除 | `DELETE /api/tasks/{id}`：删记录、结果和 `data/results/{id}/`；支持批量删除 |

## 9. 设置与 Cookie（M1，远程配置项在 M3）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 设置 | 读写接口 | `GET/PUT /api/settings`：用 Pydantic 模型校验；返回时 Cookie 和 Token 只给掩码（`****后4位`） |
| Cookie | 粘贴 SESSDATA | 把 SESSDATA（以及 bili_jct、DedeUserID，有就一起存）写成 Netscape 格式的 `data/cookies.txt`，供 yt-dlp 的 `cookiefile` 使用 |
| Cookie | 上传 cookies.txt | `POST /api/settings/cookies`（文件上传），检查格式，只保留 `.bilibili.com` 的条目后保存 |
| Cookie | 登录校验 | `GET /api/settings/cookies/check`：带 Cookie 请求 B 站 `x/web-interface/nav`，根据 `isLogin` 返回登录状态和昵称；SESSDATA 过期时提示重新获取 |
| 设置 | 识别配置 | 默认后端（local/remote）、默认模型、默认开不开说话人分离、默认强制转录、音频缓存策略 |
| 设置 | 远程配置 | worker 地址（默认 `http://127.0.0.1:9000`）、Token；“测试连接”按钮调用 health 接口 |
| 设置 | 热词组 | `hotword_groups: [{name, words[]}]`，可以增删改；提交任务时多选热词组，和临时填的热词合并 |
| 设置 | 安全 | 服务只绑定 `127.0.0.1`；`data/` 目录加入 `.gitignore`；Cookie 和 Token 不写进日志 |

## 10. 前端页面（M1，远程相关在 M3）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 前端框架 | 页面结构 | `web/index.html` 引入本地的 `vendor/vue.global.prod.js`；`app.js` 用 `createApp` 写单文件应用；用 hash 路由（`#/`、`#/task/:id`、`#/history`、`#/settings`） |
| 前端框架 | 接口封装 | `api.js`：基于 fetch 封装，统一处理错误码并弹 toast；`sse(id, onEvent)` 封装 EventSource |
| 提交页 | 链接输入 | 大输入框，粘贴后自动调用 `/api/parse`（防抖 500ms），解析中显示骨架屏 |
| 提交页 | 视频信息与分P | 显示封面、标题、UP 主、时长；分P用复选框列表（全选、反选），每个分P带“有字幕”标签 |
| 提交页 | 参数面板 | 强制转录、说话人分离、识别后端/模型（remote 离线时置灰）、热词（标签输入 + 热词组多选）；参数记到 localStorage |
| 任务列表 | 卡片与进度 | 首页下方显示进行中和最近的任务卡片：阶段名 + 进度条 + 预计剩余时间；订阅 `/api/events` 实时更新；有取消和重试按钮 |
| 结果页 | 按句显示 | 每句一行：`[mm:ss]` 时间戳 + 文本（有说话人时用颜色区分）；点时间戳在新标签页打开 B 站对应时间点 |
| 结果页 | 操作 | 复制全文（Clipboard API）、下载 txt/srt/md、重新转录；顶部显示来源和模型标签 |
| 结果页 | 搜索与高亮 | 页内搜索框，匹配的句子高亮，上下切换结果 |
| 历史页 | 列表 | 搜索、按状态筛选、分页；可以多选，批量导出 zip 或批量删除 |
| 设置页 | 表单 | Cookie（粘贴 / 上传 + 校验状态）、识别默认值、远程地址 / Token / 测试连接、热词组管理、清理缓存 |
| 全局 | 状态栏 | 顶部显示：模型加载状态、远程状态灯（在线/离线/未配置）、Cookie 登录状态 |
| 全局 | 主题与适配 | 用 CSS 变量定义明暗主题，跟随系统；宽度小于 768px 时改成单列布局 |

## 11. 远程 asr-worker（M2）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| worker | 服务 | `worker/server.py`：FastAPI 服务，启动时就把 paraformer（+cam++）加载到 GPU（`device="cuda:0"`）；Nano 模型等第一次请求时再加载 |
| worker | 转录接口 | `POST /transcribe`：接收文件存到临时目录 → 用 `asyncio.Lock` 保证一次只跑一个 GPU 任务 → 调 `run_in_threadpool(model.generate, ...)` → 返回统一结构 → 删掉临时文件 |
| worker | 共用代码 | 把句子转换的逻辑抽到 `common/asr_format.py`，本地和 worker 都引用，保证两边输出一致 |
| worker | Fun-ASR-Nano | 单独写一个适配器；先验证它能不能输出句级或字级时间戳：能就转成 sentences，不能就返回整段文本并标记 `no_timestamp`，前端提示“该模型不支持 srt” |
| worker | 鉴权 | 中间件检查 `Authorization: Bearer`，Token 从环境变量 `ASR_TOKEN` 读；`/health` 也要鉴权 |
| worker | 健康检查 | `GET /health` 返回 `{gpu: torch.cuda.get_device_name(), mem_used, models_loaded, busy}` |
| worker | 部署脚本 | `worker/setup.sh`：建 venv → 安装和 AutoDL 镜像 CUDA 版本匹配的 torch → `pip install -r requirements.txt` → `export MODELSCOPE_CACHE=/root/autodl-tmp/modelscope`（数据盘）→ 预下载模型 |
| worker | 启动 | `worker/run.sh`：在 tmux 里用 uvicorn 监听 `127.0.0.1:9000`，日志写到 `/root/autodl-tmp/worker.log` |
| worker | 连接方式 | README 写明本地执行 `ssh -CNg -L 9000:127.0.0.1:9000 root@<AutoDL地址> -p <端口>`；v1 由用户手动建隧道，以后再考虑在工具里管理隧道 |

## 12. 错误处理与日志（贯穿各里程碑）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 错误处理 | 统一错误码 | `errors.py` 定义 `AppError(code, message, hint)`：`INVALID_URL`、`VIDEO_NOT_FOUND`、`LOGIN_REQUIRED`、`REGION_BLOCKED`、`RISK_CONTROL(412)`、`DOWNLOAD_FAILED`、`FFMPEG_FAILED`、`MODEL_LOAD_FAILED`、`REMOTE_UNAUTHORIZED`、`REMOTE_UNAVAILABLE`、`CANCELLED` |
| 错误处理 | yt-dlp 错误映射 | 捕获 `DownloadError`，根据报错关键字映射到上面的错误码，`hint` 里给出操作建议（比如“请在设置里配置 Cookie”） |
| 错误处理 | 前端展示 | 失败的任务卡片显示 message + hint，带“重试”和“查看日志”按钮 |
| 日志 | 任务日志 | 每个任务的阶段日志写进 `data/logs/tasks/{id}.log`，前端可以查看；Cookie 和 Token 脱敏 |
| 日志 | 服务日志 | 用 `logging` + `RotatingFileHandler` 写 `data/logs/app.log`（10 MB × 5） |

## 13. 启动与环境（M1）

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 启动 | start.bat | 检查有没有 `uv`，没有就提示安装命令 → `uv sync` → `uv run python -m app.main` |
| 启动 | 依赖检查 | 启动时检查 `ffmpeg` 和 `ffprobe` 是否可用，缺了就在控制台和页面顶部显示安装提示；同时打印 yt-dlp 版本 |
| 启动 | 端口与浏览器 | 默认端口 8765，被占用就依次试 8766–8775；服务就绪后用 `webbrowser.open` 打开页面 |
| 启动 | 依赖管理 | `pyproject.toml` 配置 torch CPU 版的源（`[tool.uv.sources]` 指向 PyTorch CPU index），避免装上巨大的 CUDA 包 |
| 维护 | 更新 yt-dlp | 设置页加一个“更新下载组件”按钮，执行 `uv pip install -U yt-dlp`，提示重启后生效 |

## 14. 测试与验收

| 模块 | 功能点 | 开发方案 |
|---|---|---|
| 单元测试 | 纯函数 | 用 pytest 测：链接规范化（各种输入格式）、字幕 JSON/SRT 解析、srt 时间格式和长句拆分、txt 分段规则、cookies.txt 生成 |
| 集成测试 | 识别链路 | 准备一段 30 秒中文样例音频（自己录，放到 `tests/fixtures`），测 LocalASR 输出结构正确、热词生效 |
| 集成测试 | 远程 | 用 mock 的 worker（FastAPI TestClient）测上传、401、超时退回本地这几条路径 |
| 手工验收 | M1 | 单P有 CC 字幕、单P没有字幕、多P合集、需要登录的视频、短链分享文案，各跑通一次；三种格式都能正常导出 |
| 手工验收 | M2/M3 | 在 AutoDL 上部署 worker → 建 SSH 隧道 → 页面状态灯显示在线 → 1 小时视频用 GPU 跑完；断开隧道后再提交，确认自动退回本地 |
