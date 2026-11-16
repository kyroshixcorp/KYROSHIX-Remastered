#!/usr/bin/env bash
# Read-only diagnostic for KYROSHIX BOT inside a Linux VM.
set -u

section() { printf '\n===== %s =====\n' "$1"; }
run() {
  if command -v "$1" >/dev/null 2>&1; then
    shift
    "$@" 2>&1 || true
  else
    printf 'NOT INSTALLED: %s\n' "$1"
  fi
}

section "OS / KERNEL"
cat /etc/os-release 2>/dev/null || true
uname -a
printf '\nVirtualization hints:\n'
systemd-detect-virt 2>/dev/null || true

section "CPU / MEMORY"
lscpu 2>/dev/null | grep -E 'Model name|CPU\(s\)|Thread|Core|Socket|Virtualization' || true
free -h 2>/dev/null || true

section "PCI GRAPHICS"
if command -v lspci >/dev/null 2>&1; then
  lspci -nnk 2>/dev/null | grep -A4 -Ei 'VGA compatible|3D controller|Display controller|Audio device' || true
else
  echo "pciutils/lspci not installed"
fi

section "GPU DEVICE NODES"
ls -l /dev/kfd /dev/dri 2>&1 || true
ls -l /dev/dri/by-path 2>&1 || true
id
for group in render video; do
  getent group "$group" 2>/dev/null || true
done

section "ROCM"
if command -v rocminfo >/dev/null 2>&1; then
  rocminfo 2>&1 | grep -E 'Name:|Marketing Name:|gfx[0-9]+' | head -80 || true
else
  echo "rocminfo not installed"
fi
if command -v rocm-smi >/dev/null 2>&1; then
  rocm-smi 2>&1 || true
fi

section "VULKAN"
if command -v vulkaninfo >/dev/null 2>&1; then
  vulkaninfo --summary 2>&1 | head -100 || true
else
  echo "vulkan-tools not installed"
fi

section "OLLAMA"
if command -v ollama >/dev/null 2>&1; then
  ollama --version 2>&1 || true
  ollama list 2>&1 || true
  curl --max-time 3 -sS http://127.0.0.1:11434/api/tags || true
  printf '\n'
else
  echo "Ollama not installed"
fi

section "PYTHON / PYTORCH"
python3 --version 2>&1 || true
python3 - <<'PY' 2>&1 || true
try:
    import torch
    print("torch:", torch.__version__)
    print("torch.cuda.is_available:", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("device:", torch.cuda.get_device_name(0))
except Exception as exc:
    print("PyTorch check:", repr(exc))
PY

section "NETWORK"
ip -brief address 2>/dev/null || true
ip route 2>/dev/null || true

section "OSC PORTS"
ss -lunp 2>/dev/null | grep -E ':(9000|9001)\b' || echo "No UDP listener found on 9000/9001"

echo
echo "Diagnostic complete. No settings or files were modified."
