#!/bin/bash
# Installs Whisper and PyTorch so transcription works.
# Kept OUT of the base install: it is 2-3GB, and plenty of people never use it.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

[ -x venv/bin/python ] || { echo "  [!] Run Start Zerko.bat once first."; exit 1; }

echo ""
echo "  Checking for an NVIDIA GPU..."
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    echo "  Found: $GPU"
    echo "  Installing the CUDA build of PyTorch (this is the big download)..."
    venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu121
else
    echo "  No NVIDIA GPU visible to WSL."
    echo "  Installing the CPU build - transcription will work, but slowly."
    venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
fi

echo "  Installing Whisper..."
venv/bin/pip install openai-whisper

echo ""
venv/bin/python - <<'PY'
try:
    import torch, whisper
    print(f"  torch   : {torch.__version__}")
    print(f"  CUDA    : {'yes - ' + torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no (CPU only)'}")
    print("  whisper : installed")
    print("")
    print("  Transcription is ready. Restart Zerko, then use Transcribe on a clip.")
except Exception as e:
    print(f"  [!] Something did not install cleanly: {e}")
PY
echo ""
