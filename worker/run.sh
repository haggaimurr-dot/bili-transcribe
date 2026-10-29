#!/usr/bin/env bash
# 后台启动 worker，监听 127.0.0.1:9000（通过 SSH 隧道访问）。有 tmux 用 tmux，没有就用 nohup。
set -euo pipefail
cd "$(dirname "$0")/.."
DATA=/root/autodl-tmp
SESSION=asr-worker
LOG=$DATA/worker.log
PIDFILE=$DATA/worker.pid

if [ ! -f worker/.env ]; then
  echo "没有 worker/.env，请先执行 bash worker/setup.sh"
  exit 1
fi

CMD="set -a; source worker/.env; set +a; \
  export MODELSCOPE_CACHE=$DATA/modelscope; \
  source .venv-worker/bin/activate; \
  exec uvicorn worker.server:app --host 127.0.0.1 --port 9000"

if command -v tmux >/dev/null 2>&1; then
  if tmux has-session -t $SESSION 2>/dev/null; then
    echo "worker 已在运行：tmux attach -t $SESSION"
    exit 0
  fi
  tmux new-session -d -s $SESSION "bash -c '$CMD' 2>&1 | tee -a $LOG"
  echo "已启动（tmux 会话 $SESSION），日志：$LOG"
  echo "查看：tmux attach -t $SESSION    停止：tmux kill-session -t $SESSION"
else
  if [ -f $PIDFILE ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
    echo "worker 已在运行（PID $(cat $PIDFILE)）"
    exit 0
  fi
  nohup bash -c "$CMD" >>$LOG 2>&1 &
  echo $! > $PIDFILE
  echo "已启动（nohup，PID $!），日志：$LOG"
  echo "查看：tail -f $LOG    停止：kill \$(cat $PIDFILE)"
fi
