# B站视频文本转录工具 · 技术方案

> 版本：v1.0　日期：2026-10-29

## 1. 目标

本地启动、带网页界面的工具：输入 B 站链接，输出带标点、带时间戳的文本（txt / srt / md）。

- 页面和下载都在本地运行；识别可以在本地 CPU 上跑，也可以交给远程 GPU（AutoDL）
- 视频有字幕就直接用字幕，没有才转录
- 中文准确率优先：用 FunASR，支持热词、VAD 分段和标点恢复

## 2. 整体架构

```
浏览器页面 (127.0.0.1:8765)
   │  提交链接 / SSE 实时进度 / 查看·复制·下载结果
   ▼
本地 FastAPI 服务（单进程）
   ├─ ① 解析：yt-dlp 处理 BV/av/b23.tv 短链和分P，取标题、时长
   ├─ ② 字幕优先：先找 CC 字幕，再找 AI 字幕；都没有，或开启了“强制转录”，就进入下一步
   ├─ ③ 下载音频：bestaudio(m4a) → ffmpeg 转成 16k 单声道（上传前压成 opus）
   ├─ ④ 识别（设置页切换）
   │     ├─ local ：本机 CPU 跑 paraformer-zh（兜底）
   │     └─ remote：HTTP → SSH 隧道 → AutoDL 上的 asr-worker
   └─ ⑤ 输出：txt / srt / md，写入 SQLite 历史记录

AutoDL GPU 容器：asr-worker（独立的 FastAPI 服务）
   POST /transcribe  音频 + 热词 + 参数 → 带时间戳的句子列表
   GET  /health      GPU 状态、已加载的模型
   模型：paraformer-zh（默认）/ Fun-ASR-Nano（方言、嘈杂音频，可选）
```

## 3. 前端

| 技术组合 | 原因 | 备注 |
|---|---|---|
| 单个 HTML + Vue 3（global build 本地 vendor 文件） | 不需要构建，FastAPI 直接托管；不用装 npm，离线也能用 | 页面只有：任务提交、进度、结果、历史、设置，复杂度不需要 Vite |
| 原生 CSS（CSS 变量 + 明暗主题） | 页面少，不值得引入 UI 框架 | 适配窄屏，方便手机在局域网内访问 |
| EventSource（SSE）接收进度 | 单向推送就够了，比 WebSocket 简单，断线浏览器会自动重连 | 推送各阶段（解析/下载/识别/完成）和百分比 |
| fetch 调用 REST 接口 | 浏览器原生支持，不加依赖 | 接口同时供以后其他项目调用 |
| Clipboard API + Blob 下载 | 一键复制全文、下载 txt/srt/md | 只在 localhost 下用，满足安全上下文要求 |
| localStorage 存界面偏好 | 记住上次的热词、输出格式、识别后端选择 | 只存偏好；任务数据存后端 SQLite |

**页面功能**

- 输入链接 → 解析出分P列表（可以勾选）→ 选择是否强制转录、填热词、选是否区分说话人 → 提交
- 任务卡片：显示阶段和进度条；完成后可以按句看结果（时间戳可点击）、复制、下载
- 历史记录：查看、重新导出、删除
- 设置：B 站 Cookie（粘贴 SESSDATA，或上传 cookies.txt）、识别后端（local/remote）、远程地址和 Token、常用热词组

## 4. 后端

| 技术组合 | 原因 | 备注 |
|---|---|---|
| Python 3.12 + FastAPI + uvicorn | FunASR 和 yt-dlp 都是 Python 库，同一个进程直接调用；FastAPI 自带接口文档 | 只监听 127.0.0.1:8765 |
| yt-dlp（作为 Python 库调用） | B 站支持完善：短链、分P、字幕、只下音频 | B 站接口一变就 `pip install -U yt-dlp` |
| Cookie：SESSDATA 或 cookies.txt | Windows 上新版 Chrome 用了 App-Bound 加密，`--cookies-from-browser chrome` 经常读不到 | AI 字幕、高码率、部分视频需要登录；Cookie 只存在本地 |
| ffmpeg（系统已装 8.1） | 统一转成 16k 单声道 wav；上传远程前压成 opus（1 小时约 20–30 MB） | 用 subprocess 调用 |
| FunASR：paraformer-zh（SeACo）+ fsmn-vad + ct-punc | 中文准确率高、标点自然、支持热词和句级时间戳；非自回归，CPU 上也能跑 | 首次运行从 ModelScope 下载模型，约 1 GB，之后用本地缓存 |
| cam++ 说话人分离（可选） | 访谈、对话类视频需要区分说话人 | 会明显变慢，默认关闭，按任务开启 |
| Fun-ASR-Nano（仅 remote） | 方言、口音重、嘈杂音频更准 | 基于 LLM，需要 GPU；它的时间戳粒度能不能生成 srt，要在开发时验证 |
| 识别后端抽象：`LocalASR` / `RemoteASR` | 两种实现返回同样格式（句子 + 起止时间 + 说话人），后面的导出逻辑只写一份 | 远程连不上时自动退回本地 |
| 任务队列：单个后台工作线程 + 内存队列 | 识别吃满算力，串行处理最稳；不需要 Celery/Redis | 服务重启后，未完成的任务从 SQLite 恢复为“失败，可重试” |
| SQLite（标准库 sqlite3） | 存历史任务、结果、设置；不需要单独装数据库 | 结果文件放在 `data/` 目录 |
| sse-starlette | 给前端推送任务进度 | |
| uv 管理依赖 + `start.bat` | 一键启动：首次自动建虚拟环境、装依赖，然后启动服务并打开浏览器 | torch 本地装 CPU 版 |

## 5. 远程 GPU：AutoDL asr-worker

| 技术组合 | 原因 | 备注 |
|---|---|---|
| AutoDL 按小时租的容器（如 RTX 4090 或同档） | 国内节点，ModelScope 下载模型快，按需开关机 | 用完就关机，停机期间只收存储费 |
| worker：FastAPI + FunASR（CUDA 版 torch） | 和本地共用识别代码，模型常驻显存 | 代码放在仓库的 `worker/` 目录，用 `setup.sh` 一键部署 |
| SSH 隧道 `ssh -L 9000:127.0.0.1:9000` | 不对公网暴露端口，最安全 | 本地工具检测隧道和 `/health`，界面上显示连接状态 |
| Bearer Token 鉴权 | 防止误用；就算以后开了 AutoDL 的自定义端口映射也有一层保护 | Token 存在本地设置里 |
| 模型缓存放在数据盘 | 重新开机不用再下载模型 | 设置 `MODELSCOPE_CACHE` 指向数据盘 |

**性能预估（未实测）**：paraformer 识别 1 小时音频，GPU 上预计不到 1 分钟，CPU 上预计几分钟。两者准确率一样，GPU 只是更快；真正需要 GPU 的是 Fun-ASR-Nano 这类 LLM 模型。

## 6. 接口草案

| 接口 | 说明 |
|---|---|
| `POST /api/parse` | 解析链接，返回标题、分P列表、是否有字幕 |
| `POST /api/tasks` | 创建任务（分P、热词、强制转录、说话人分离、识别后端） |
| `GET  /api/tasks` / `GET /api/tasks/{id}` | 任务列表 / 任务详情和结果 |
| `GET  /api/tasks/{id}/events` | SSE 进度推送 |
| `GET  /api/tasks/{id}/export?fmt=txt\|srt\|md` | 导出结果 |
| `DELETE /api/tasks/{id}` | 删除任务 |
| `GET/PUT /api/settings` | 设置：Cookie、后端、远程地址、Token、热词组 |
| `GET  /api/remote/health` | 检查远程 worker 状态 |

## 7. 目录结构

```
bili-transcribe/
├─ app/                  # 本地服务
│  ├─ main.py            # FastAPI 入口、静态托管
│  ├─ api.py             # 路由
│  ├─ tasks.py           # 队列与工作线程
│  ├─ bili.py            # yt-dlp 解析、字幕、下载
│  ├─ asr/               # base.py / local.py / remote.py
│  ├─ export.py          # txt / srt / md
│  └─ db.py              # SQLite
├─ web/                  # index.html、app.js、style.css、vendor/vue.global.prod.js
├─ worker/               # AutoDL 端：server.py、setup.sh、requirements.txt
├─ data/                 # 数据库、音频缓存、结果（gitignore）
├─ pyproject.toml
└─ start.bat
```

## 8. 开发顺序

1. **本地完整版（CPU）**：解析 → 字幕优先 → 下载 → paraformer 识别 → 导出，页面全部跑通
2. **识别后端抽象 + worker**：写 `worker/` 服务和 `setup.sh`，在 AutoDL 上验证
3. **远程接入**：设置页、SSH 隧道检测、自动退回本地、模型选择（含 Fun-ASR-Nano 验证）

**暂不做**：LLM 总结、其他平台、RunPod Serverless、多用户。
