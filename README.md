# 视频文本转录（B 站 / YouTube / 本地文件）

本地启动、带网页界面：粘贴 B 站或 YouTube 链接（也可以上传本地音视频）→ 有字幕直接用字幕，没有就用 FunASR 转录 → 输出 txt / srt / md。中文用 paraformer-zh（支持热词和说话人分离），英、日、韩、粤用 SenseVoiceSmall。

## 启动

双击 `start.bat`（需要先装 [uv](https://docs.astral.sh/uv/) 和 ffmpeg）。首次会安装依赖、第一次识别时下载约 2 GB 识别模型（存到 `~/.cache/modelscope`）。服务启动后会自动打开 http://127.0.0.1:8765 。

本机 CPU 实测 RTF 约 0.33：1 小时视频大约要识别 20 分钟。长视频建议用远程 GPU。

```bash
uv sync
uv run python -m app.main            # 加 --no-browser 不自动打开浏览器
uv run pytest -q                     # 测试
```

## 首次使用必做：配置 B 站 Cookie

B 站对未登录的请求会返回 412（风控）。在浏览器登录 B 站 → F12 → 应用 → Cookie → 复制 `SESSDATA`，到页面「设置 → B 站 Cookie」粘贴保存；或上传浏览器扩展导出的 cookies.txt。Cookie 只保存在本机 `data/cookies.txt`。

## YouTube

国内需要代理：到「设置 → YouTube」填写代理地址（例如 Clash Verge 的 `http://127.0.0.1:7897`），点「测试代理」。只有 YouTube 的请求走这个代理。解析 YouTube 需要本机装有 Node.js（`yt-dlp-ejs` 用它求解 YouTube 的 JS 挑战）。如果遇到「Sign in to confirm you're not a bot」，在同一页上传从 youtube.com 导出的 cookies.txt。

字幕优先级：人工字幕（原语言 → 中文 → 英文）→ 原语言自动字幕 → 转录。自动字幕没有标点，结果页可以点「重新转录」。

## 远程 GPU

见 [worker/README.md](worker/README.md)：在 AutoDL 部署 asr-worker → 本地建 SSH 隧道 → 设置页填地址和 Token。远程不可用时自动退回本地识别。

**日常使用（部署好之后）**：在 AutoDL 控制台点「开机」，然后双击 `connect-gpu.bat`。它会：
1. 问一次登录指令（直接回车沿用上次；开机后端口变了就粘贴控制台新的「登录指令」），保存在 `data/remote_gpu.json`；
2. 第一次使用时生成本机 SSH 密钥并装到 AutoDL 上（只输这一次密码），以后免密；
3. 远程执行 `bash worker/run.sh` 启动 worker；
4. 建隧道 `127.0.0.1:9000 → AutoDL:9000`，等 worker 预加载完模型（约 2～3 分钟）；
5. 本地工具没运行就自动打开 `start.bat`，否则直接打开页面。

这个窗口就是隧道：用远程 GPU 时保持打开，断线会自动重连；用完关掉窗口，再去控制台关机。

## 目录

```
app/      本地服务（FastAPI）    web/     页面（Vue 3，无构建）
common/   本地与 worker 共用的结果格式化
worker/   AutoDL 端识别服务      tests/   pytest
data/     数据库、缓存、结果、日志（已 gitignore）
```
