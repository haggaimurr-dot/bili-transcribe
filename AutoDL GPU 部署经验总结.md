# AutoDL GPU 部署 asr-worker 经验总结

> 日期：2026-10-29　实例：AutoDL vGPU-32GB（`/health` 报告为 NVIDIA GeForce RTX 4080 SUPER），¥1.58/小时
> 结果：paraformer-zh worker 已在 GPU 上运行，本地工具「测试连接」显示在线，延迟 190ms

## ⚠️ 先说明：Fun-ASR-Nano 还没有装

这次部署实际装上、跑通的是 **paraformer-zh 全套**（paraformer-zh + fsmn-vad + ct-punc + cam++）。

**Fun-ASR-Nano 没有预装，也没有在 GPU 上验证过。** worker 代码里有 Nano 的加载逻辑：页面选这个模型时，首次请求才会下载并加载。但模型 ID、加载参数、是否输出时间戳，都还没确认。验证步骤见第 5 节。

## 1. 最终可用的部署流程

### AutoDL 端（首次）

```bash
source /etc/network_turbo                      # 学术加速，clone GitHub 用
cd /root/autodl-tmp
git clone https://github.com/haggaimurr-dot/bili-transcribe.git
cd bili-transcribe
unset http_proxy https_proxy                   # 关掉加速，后面走清华源和 ModelScope
apt-get update && apt-get install -y tmux      # 可选；没有 tmux 时 run.sh 会改用 nohup
bash worker/setup.sh                           # 装依赖、预下载模型、生成 Token
bash worker/run.sh                             # 后台启动
curl -s http://127.0.0.1:9000/health           # 返回 {"detail":"unauthorized"} 说明正常
cat worker/.env                                # 取 Token
```

### 本地端

```bash
# 本地 PowerShell 里运行，窗口保持打开
ssh -CN -o ServerAliveInterval=30 -L 9000:127.0.0.1:9000 -p <端口> root@<地址>

# 另开一个窗口验证（注意要写 curl.exe）
curl.exe http://127.0.0.1:9000/health
```

在工具的「设置 → 远程 GPU」里填 `http://127.0.0.1:9000` 和 Token → 测试连接 → **保存**。

### 下次开机

```bash
cd /root/autodl-tmp/bili-transcribe && bash worker/run.sh
```

本地重新建隧道就行。Token 和模型缓存都在数据盘上，不会丢。

## 2. 镜像和配置的选择

| 项 | 选择 | 原因 |
|---|---|---|
| 镜像 | 基础镜像 → PyTorch 最新版 / Python 3.12 / CUDA 最高版本 | 自带 GPU 版 torch，`setup.sh` 用 `--system-site-packages` 直接复用，不用重新下载几个 GB；新卡要配新版 CUDA |
| GPU 数量 | 1 | worker 用锁保证一次只跑一个任务 |
| 数据盘 | 免费 50GB，不扩容 | paraformer 全套约 2 GB；模型缓存放在 `/root/autodl-tmp/modelscope` |

## 3. 踩过的坑

| # | 现象 | 原因 | 解决 |
|---|---|---|---|
| 1 | `ssh: connect to host ... port 41234: Connection refused` | 直接用了文档里的**示例**端口和地址 | 从 AutoDL 控制台 → 实例 → 「SSH 登录」复制自己实例的登录指令 |
| 2 | 隧道命令在 `root@autodl-container` 提示符下执行 | SSH 隧道要在**本地电脑**上建，不是在服务器上 | 在本地 PowerShell 里执行 |
| 3 | 本地 `curl.exe` 报 `Recv failure: Connection was reset` | 隧道已经建好，但 AutoDL 上 9000 端口没有服务：worker 没启动 | 在 AutoDL 上检查 worker 进程和日志 |
| 4 | `tmux: command not found` | 这个 PyTorch 镜像没装 tmux，`run.sh` 一开头就失败了 | `apt-get install -y tmux`；`run.sh` 已改为没有 tmux 时用 nohup 兜底（提交 a89b549） |
| 5 | PowerShell 执行 `curl -s ...` 后停住，提示输入 `Uri:` | PowerShell 的 `curl` 是 `Invoke-WebRequest` 的别名 | 按 Ctrl+C 退出；本地要写 `curl.exe` |
| 6 | 一键命令中途停住，后面的步骤都没执行 | 命令用 `&&` 连接，前面一步失败后面就不会执行 | 先 `ls worker/.env` 确认 `setup.sh` 跑完了没有 |
| 7 | SSH 密码发进了聊天 | — | 密码等于已经泄露，可以的话在控制台重置；以后只粘贴到终端 |
| 8 | 点了「测试连接」显示在线，但其实没保存 | 测试和保存是两个按钮 | 测试通过后记得点「保存」 |

## 4. 排查顺序（从远到近）

1. **AutoDL 上 worker 在不在**：`tmux ls`，或者 `cat /root/autodl-tmp/worker.pid`。
2. **AutoDL 上直接请求**：`curl -s http://127.0.0.1:9000/health`。返回 `unauthorized` 就正常；连接失败就看 `tail -50 /root/autodl-tmp/worker.log`。
3. **隧道通不通**：本地 `curl.exe http://127.0.0.1:9000/health`。
   - 返回 `unauthorized`：隧道正常。
   - `Connection was reset`：隧道是通的，问题在第 1、2 步。
   - `Failed to connect`：隧道没建，或者隧道窗口被关了。
4. **Token 对不对**：设置页「测试连接」提示「Token 错误」，就重新 `cat worker/.env` 复制。

## 5. Fun-ASR-Nano：待验证

worker 里的写法（`worker/server.py`）：

```python
AutoModel(model=NANO_MODEL, vad_model="fsmn-vad",
          vad_kwargs={"max_single_segment_time": 30000},
          trust_remote_code=True, device="cuda:0")
# NANO_MODEL 默认 "FunAudioLLM/Fun-ASR-Nano-2512"，可用环境变量覆盖
```

建议的验证步骤（在 AutoDL 上执行）：

```bash
cd /root/autodl-tmp/bili-transcribe && source .venv-worker/bin/activate
export MODELSCOPE_CACHE=/root/autodl-tmp/modelscope
python - <<'PY'
from funasr import AutoModel
m = AutoModel(model="FunAudioLLM/Fun-ASR-Nano-2512", vad_model="fsmn-vad",
              vad_kwargs={"max_single_segment_time": 30000}, trust_remote_code=True, device="cuda:0")
import glob
wav = glob.glob("/root/autodl-tmp/modelscope/**/asr_example.wav", recursive=True)[0]
res = m.generate(input=wav, batch_size_s=300)
print(res)
PY
```

需要确认的点：

| 问题 | 怎么判断 | 不行时的处理 |
|---|---|---|
| 模型 ID 对不对 | 能否下载 | 去 ModelScope 搜 Fun-ASR-Nano，用 `NANO_MODEL` 环境变量指定正确的 ID |
| 是否要额外的 `remote_code` 文件 | 报 `trust_remote_code` 或找不到模型类 | 按模型主页的说明补上 `remote_code` 参数 |
| 有没有句级时间戳 | `res` 里有没有 `sentence_info` 或 `timestamp` | 没有就只能输出整段文本，srt 不可用（页面已有提示） |
| 显存占用 / 速度 | 运行时执行 `nvidia-smi` | 32 GB 显存预计足够 |

验证通过后，把 Nano 加进 `setup.sh` 的预下载步骤，再更新这份文档。

## 6. 费用习惯

- 按小时计费，**用完在控制台关机**。关机后数据盘保留，代码、模型、Token 都在。
- 部署时间大部分花在下载依赖和模型上。数据盘缓存好之后，开机到可用只要 1 分钟左右。
