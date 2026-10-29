#!/usr/bin/env bash
# AutoDL 一键部署：在仓库根目录执行 bash worker/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

DATA=/root/autodl-tmp
export MODELSCOPE_CACHE=$DATA/modelscope
mkdir -p "$MODELSCOPE_CACHE"

# AutoDL 镜像一般已带 conda/python 和匹配 CUDA 的 torch；用 --system-site-packages 复用它
if [ ! -d .venv-worker ]; then
  python3 -m venv --system-site-packages .venv-worker
fi
source .venv-worker/bin/activate
pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple

if ! python -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
  CUDA_VER=$(nvidia-smi | grep -oP 'CUDA Version: \K[0-9]+\.[0-9]+' || echo "12.1")
  case "$CUDA_VER" in
    11.*) IDX=cu118 ;;
    12.[0-3]) IDX=cu121 ;;
    *) IDX=cu124 ;;
  esac
  echo "安装 torch（$IDX，驱动 CUDA $CUDA_VER）"
  pip install torch torchaudio --index-url https://download.pytorch.org/whl/$IDX
fi

pip install -r worker/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 预下载模型到数据盘，重新开机不用再下载
python - <<'PY'
from funasr import AutoModel
AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc", spk_model="cam++",
          disable_update=True)
print("paraformer-zh / fsmn-vad / ct-punc / cam++ 已就绪")
PY

if [ ! -f worker/.env ]; then
  echo "ASR_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')" > worker/.env
  echo "已生成 worker/.env，Token："
  cat worker/.env
fi
echo "部署完成，执行 bash worker/run.sh 启动"
