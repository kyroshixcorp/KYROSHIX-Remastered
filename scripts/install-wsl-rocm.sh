#!/usr/bin/env bash
set -euo pipefail

# Install the project into a dedicated WSL ROCm environment. The project
# currently pins PyTorch to CPU wheels for its Hyper-V/no-passthrough profile;
# after uv sync, replace those wheels with AMD's supported ROCm 7.2 builds.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
  echo "ERROR: run this script inside WSL2 Ubuntu 24.04." >&2
  exit 1
fi

if ! grep -qE '^VERSION_ID="24\.04"' /etc/os-release; then
  echo "ERROR: this profile is pinned to Ubuntu 24.04." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is required. Install uv first: https://docs.astral.sh/uv/" >&2
  exit 1
fi

export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-.venv-wsl-rocm}"
VENV_PYTHON="$UV_PROJECT_ENVIRONMENT/bin/python"

if ! command -v rocminfo >/dev/null 2>&1; then
  echo "ERROR: rocminfo not found. Install AMD WSL runtime first (see docs/WSL2-ROCM-RX7900XTX.md)." >&2
  exit 1
fi

if ! rocminfo | grep -q 'gfx1100'; then
  echo "ERROR: ROCm did not expose gfx1100. Check the Windows WSL driver and AMD runtime." >&2
  exit 1
fi

echo "[1/3] Syncing project into isolated environment: $UV_PROJECT_ENVIRONMENT"
uv sync --python 3.12

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "ERROR: expected Python environment not found: $VENV_PYTHON" >&2
  exit 1
fi

echo "[2/3] Installing AMD ROCm 7.2 PyTorch wheels (Python 3.12)"
uv pip install --python "$VENV_PYTHON" --reinstall \
  "numpy==1.26.4" \
  "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/torch-2.9.1%2Brocm7.2.0.lw.git7e1940d4-cp312-cp312-linux_x86_64.whl" \
  "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/torchvision-0.24.0%2Brocm7.2.0.gitb919bd0c-cp312-cp312-linux_x86_64.whl" \
  "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/torchaudio-2.9.0%2Brocm7.2.0.gite3c6ee2b-cp312-cp312-linux_x86_64.whl" \
  "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/triton-3.5.1%2Brocm7.2.0.gita272dfa8-cp312-cp312-linux_x86_64.whl"

echo "[3/3] Verifying PyTorch ROCm and RX 7900 XTX"
"$VENV_PYTHON" - <<'PY'
import torch

print("PyTorch:", torch.__version__)
print("HIP:", torch.version.hip)
if not torch.cuda.is_available():
    raise SystemExit("ERROR: PyTorch cannot access the ROCm GPU")
print("GPU:", torch.cuda.get_device_name(0))
if "7900 XTX" not in torch.cuda.get_device_name(0):
    raise SystemExit("ERROR: the detected GPU is not the expected RX 7900 XTX")
x = torch.randn((1024, 1024), device="cuda")
print("Tensor test:", (x @ x).device)
PY

echo "ROCm WSL environment ready: $UV_PROJECT_ENVIRONMENT"
echo "Run with: UV_PROJECT_ENVIRONMENT=$UV_PROJECT_ENVIRONMENT uv run --no-sync python main.py"
