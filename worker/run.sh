#!/usr/bin/env bash
# 在 tmux 里启动 worker，监听 127.0.0.1:9000（通过 SSH 隧道访问）
set -euo pipefail
cd "$(dirname "$0")/.."
DATA=/root/autodl-tmp
SESSION=asr-worker

if tmux has-session -t $SESSION 2>/dev/null; then
  echo "worker 已在运行：tmux attach -t $SESSION"
  exit 0
fi

tmux new-session -d -s $SESSION "set -a; source worker/.env; set +a; \
  export MODELSCOPE_CACHE=$DATA/modelscope; \
  source .venv-worker/bin/activate; \
  uvicorn worker.server:app --host 127.0.0.1 --port 9000 2>&1 | tee -a $DATA/worker.log"
echo "已启动（tmux 会话 $SESSION），日志：$DATA/worker.log"
echo "查看：tmux attach -t $SESSION    停止：tmux kill-session -t $SESSION"
