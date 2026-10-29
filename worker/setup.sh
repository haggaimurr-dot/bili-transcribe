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

# PyTorch 镜像一般只带 torch；Nano 的时间戳（forced_align）要和 torch 版本完全对应的 torchaudio
if ! python -c "import torchaudio" 2>/dev/null; then
  TV=$(python -c "import torch;print(torch.__version__.split('+')[0])")
  CU=$(python -c "import torch;print('cu'+(torch.version.cuda or '').replace('.',''))")
  echo "安装 torchaudio==$TV（$CU）"
  (source /etc/network_turbo 2>/dev/null; pip install "torchaudio==$TV" --index-url "https://download.pytorch.org/whl/$CU") \
    || echo "⚠️ torchaudio 安装失败：paraformer 仍可用（靠 kaldi-native-fbank），Fun-ASR-Nano 的时间戳不可用"
fi

pip install -r worker/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# Fun-ASR-Nano 的模型代码（model.py 等）在官方仓库里，不在 ModelScope 的模型目录里
FUN_ASR_DIR=$DATA/Fun-ASR
if [ ! -f "$FUN_ASR_DIR/model.py" ]; then
  echo "clone Fun-ASR 仓库到 $FUN_ASR_DIR"
  (source /etc/network_turbo 2>/dev/null; git clone --depth 1 https://github.com/FunAudioLLM/Fun-ASR.git "$FUN_ASR_DIR")
fi
if [ -f "$FUN_ASR_DIR/requirements.txt" ]; then
  # 跳过 torch/torchaudio，避免覆盖镜像里和 CUDA 匹配的版本
  grep -viE '^(torch|torchaudio|torchvision)([<>=~! ]|$)' "$FUN_ASR_DIR/requirements.txt" > /tmp/funasr-nano-req.txt
  pip install -r /tmp/funasr-nano-req.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
fi

# 预下载模型到数据盘，重新开机不用再下载
python - <<'PY'
from funasr import AutoModel
AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc", spk_model="cam++",
          disable_update=True)
print("paraformer-zh / fsmn-vad / ct-punc / cam++ 已就绪")
AutoModel(model="iic/SenseVoiceSmall", vad_model="fsmn-vad", disable_update=True)
print("SenseVoiceSmall（英日韩粤）已就绪")
PY

if [ ! -f worker/.env ]; then
  echo "ASR_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')" > worker/.env
  echo "已生成 worker/.env，Token："
  cat worker/.env
fi
echo "部署完成，执行 bash worker/run.sh 启动"
