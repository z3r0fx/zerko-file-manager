#!/bin/bash
# Installs a local image model for Zerko's generated Looks: ComfyUI with
# Qwen Image Edit 2511 (Apache 2.0, fine for paid work) on an NVIDIA card.
# Runs inside WSL as the normal user, no sudo. Safe to run again: finished
# steps are skipped and interrupted downloads resume.
#
#   ZK_IMAGEGEN_HOME   where it goes (default ~/zerko-imagegen)
#   ZK_QWEN_QUANT      GGUF size: Q4_K_M (12 GB cards, default), Q5_K_M, Q6_K, Q8_0

set -o pipefail
ROOT="${ZK_IMAGEGEN_HOME:-$HOME/zerko-imagegen}"
QUANT="${ZK_QWEN_QUANT:-Q4_K_M}"
COMFY="$ROOT/ComfyUI"
export PATH="$HOME/.local/bin:$PATH"

step() { echo; echo "=== $*"; }
fail() { echo; echo "FAILED: $*"; exit 1; }

step "Graphics card"
SMI=$(command -v nvidia-smi || echo /usr/lib/wsl/lib/nvidia-smi)
"$SMI" --query-gpu=name,memory.total,driver_version --format=csv,noheader \
  || fail "No NVIDIA card is visible in WSL. Update the NVIDIA driver in Windows, then run this again."

step "Disk space"
mkdir -p "$ROOT" || fail "cannot create $ROOT"
FREE=$(df -Pk "$ROOT" | awk 'NR==2{print int($4/1048576)}')
echo "free: ${FREE} GB in $ROOT"
[ "$FREE" -ge 40 ] || fail "Needs about 40 GB free, found ${FREE} GB."

step "uv (Python installer, no sudo)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh || fail "uv install"
fi
uv --version

step "ComfyUI"
if [ -d "$COMFY/.git" ] && command -v git >/dev/null; then
  git -C "$COMFY" pull --ff-only || echo "(kept the current ComfyUI)"
elif [ ! -f "$COMFY/main.py" ]; then
  if command -v git >/dev/null; then
    git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$COMFY" || fail "download ComfyUI"
  else
    curl -L --fail -o "$ROOT/comfy.tar.gz" https://github.com/comfyanonymous/ComfyUI/archive/refs/heads/master.tar.gz || fail "download ComfyUI"
    mkdir -p "$COMFY" && tar -xzf "$ROOT/comfy.tar.gz" -C "$COMFY" --strip-components=1 && rm "$ROOT/comfy.tar.gz"
  fi
fi

step "Python environment (PyTorch for RTX 50 series, CUDA 12.8)"
cd "$COMFY" || fail "no ComfyUI folder"
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv || fail "python venv"
if ! .venv/bin/python -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
  uv pip install --python .venv/bin/python torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128 || fail "PyTorch"
fi
uv pip install --python .venv/bin/python -r requirements.txt || fail "ComfyUI requirements"
.venv/bin/python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
.venv/bin/python -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" || fail "PyTorch cannot see the card"

step "GGUF loader"
GG="$COMFY/custom_nodes/ComfyUI-GGUF"
if [ ! -f "$GG/__init__.py" ]; then
  if command -v git >/dev/null; then git clone --depth 1 https://github.com/city96/ComfyUI-GGUF.git "$GG" || fail "ComfyUI-GGUF"
  else mkdir -p "$GG" && curl -L --fail https://github.com/city96/ComfyUI-GGUF/archive/refs/heads/main.tar.gz | tar -xz -C "$GG" --strip-components=1 || fail "ComfyUI-GGUF"; fi
fi
uv pip install --python .venv/bin/python -r "$GG/requirements.txt" || fail "gguf package"

get() {  # get <url> <dest>
  local url="$1" dest="$2"
  if [ -s "$dest" ]; then echo "have $(basename "$dest")"; return 0; fi
  mkdir -p "$(dirname "$dest")"
  echo "downloading $(basename "$dest")"
  for try in 1 2 3 4 5; do
    curl -L --fail -C - --retry 3 --progress-bar -o "$dest.part" "$url" 2>&1 | stdbuf -oL tr '\r' '\n' | awk 'NR%40==0 {print; fflush()}' && { mv "$dest.part" "$dest"; return 0; }
    echo "retrying ($try)"; sleep 5
  done
  fail "download $(basename "$dest")"
}

step "Models (about 23 GB)"
M="$COMFY/models"
HF=https://huggingface.co
get "$HF/unsloth/Qwen-Image-Edit-2511-GGUF/resolve/main/qwen-image-edit-2511-$QUANT.gguf" "$M/diffusion_models/qwen-image-edit-2511-$QUANT.gguf"
get "$HF/Comfy-Org/HunyuanVideo_1.5_repackaged/resolve/main/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" "$M/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors"
get "$HF/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors" "$M/vae/qwen_image_vae.safetensors"
get "$HF/lightx2v/Qwen-Image-Edit-2511-Lightning/resolve/main/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors" "$M/loras/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"

step "Start script"
cat > "$ROOT/start.sh" <<EOS
#!/bin/bash
# Starts ComfyUI for Zerko on 127.0.0.1:8188 (only this PC can reach it).
cd "$COMFY" && exec .venv/bin/python main.py --listen 127.0.0.1 --port 8188 --reserve-vram 1 --disable-auto-launch
EOS
chmod +x "$ROOT/start.sh"
echo "{\"root\": \"$ROOT\", \"quant\": \"$QUANT\", \"model\": \"qwen-image-edit-2511-$QUANT.gguf\"}" > "$ROOT/zerko.json"

step "First start"
if (exec 3<>/dev/tcp/127.0.0.1/8188) 2>/dev/null; then echo "ComfyUI is already running"; else
  nohup "$ROOT/start.sh" > "$ROOT/comfyui.log" 2>&1 &
  for i in $(seq 1 90); do sleep 2; (exec 3<>/dev/tcp/127.0.0.1/8188) 2>/dev/null && break; done
fi
curl -s http://127.0.0.1:8188/system_stats | head -c 600; echo
curl -s http://127.0.0.1:8188/object_info/UnetLoaderGGUF | grep -q UnetLoaderGGUF && echo "GGUF loader: ok" || echo "GGUF loader: MISSING (see $ROOT/comfyui.log)"
echo
echo "DONE. ComfyUI runs at http://127.0.0.1:8188 for Zerko."
