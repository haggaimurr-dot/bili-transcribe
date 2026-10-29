# asr-worker（AutoDL 部署）

1. 在 AutoDL 租一台 GPU 容器（PyTorch 镜像即可），SSH 登录后：

   ```bash
   source /etc/network_turbo   # AutoDL 学术加速，访问 GitHub 更快
   cd /root/autodl-tmp
   git clone https://github.com/haggaimurr-dot/bili-transcribe.git
   cd bili-transcribe
   bash worker/setup.sh     # 首次部署：装依赖、下载模型、生成 Token
   bash worker/run.sh       # 启动
   ```

2. 本地电脑建 SSH 隧道（地址和端口在 AutoDL 控制台「SSH 登录指令」里）：

   ```bash
   ssh -CNg -L 9000:127.0.0.1:9000 root@connect.xxx.autodl.com -p 12345
   ```

3. 本地工具「设置 → 远程 GPU」填写 `http://127.0.0.1:9000` 和 `worker/.env` 里的 `ASR_TOKEN`，点「测试连接」。

用完记得在 AutoDL 控制台关机；模型缓存在 `/root/autodl-tmp/modelscope`（数据盘），开机后直接 `bash worker/run.sh`。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ASR_TOKEN` | 空（不鉴权） | Bearer Token |
| `ASR_DEVICE` | `cuda:0` | 设备 |
| `PRELOAD_SPK` | `1` | 启动时预加载 cam++ 说话人模型 |
| `NANO_MODEL` | `FunAudioLLM/Fun-ASR-Nano-2512` | Fun-ASR-Nano 模型 ID（首次请求时加载） |
