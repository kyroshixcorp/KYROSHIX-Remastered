# KYROSHIX BOT — Ubuntu 24.04 WSL2 + ROCm 7.2 + RX 7900 XTX

Este é o perfil experimental de GPU para manter o Windows 11 como sistema principal. O WSL2 acessa a GPU AMD por meio do driver WSL do Windows; **não é PCI passthrough** e não transforma a distribuição em uma VM Hyper-V convencional.

## Arquitetura proposta

- Windows 11: sistema principal, driver AMD Adrenalin WSL2 e VRChat.
- RX 7900 XTX 24 GB: continua instalada no Windows e é exposta ao WSL2 pelo caminho oficial AMD para WSL.
- Ubuntu 24.04 WSL2: KYROSHIX BOT, Python 3.12, PyTorch ROCm e ferramentas Linux.
- Ollama: manter inicialmente no Windows 11 para servir o LLM local (por exemplo, `qwen3:8b`) à API HTTP. O WSL usa o IP do host Windows; não presumir que `localhost` funcione em toda configuração de rede.
- Branch: `experimental/ollama-local-ai`. Não faça merge na `main`.

## Matriz validada pela documentação AMD

A matriz AMD ROCm 7.2 lista explicitamente a RX 7900 XTX, Ubuntu 24.04 no WSL2 e PyTorch 2.9.1 + ROCm 7.2 como suporte de produção. O driver Windows indicado para WSL2 é AMD Software: Adrenalin Edition 26.1.1. Confirme a matriz novamente antes de atualizar versões.

## 1. Preparar o Windows 11

Abra PowerShell como Administrador:

```powershell
wsl --update
wsl --set-default-version 2
wsl --install -d Ubuntu-24.04
wsl --list --verbose
```

A distribuição Ubuntu-24.04 deve aparecer com VERSION 2. Instale o driver AMD Software: Adrenalin Edition 26.1.1 para WSL2 indicado pela documentação AMD e reinicie o Windows. Não instale drivers Linux de kernel AMD no Windows; o driver de host é o pacote WSL da AMD.

Documentação do driver: https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/install/installrad/wsl/install-radeon.html

## 2. Instalar o runtime ROCm dentro do Ubuntu

Abra Ubuntu 24.04 e execute:

```bash
sudo apt update
sudo apt install -y wget ca-certificates
wget https://repo.radeon.com/amdgpu-install/7.2/ubuntu/noble/amdgpu-install_7.2.70200-1_all.deb
sudo apt install -y ./amdgpu-install_7.2.70200-1_all.deb
sudo amdgpu-install -y --usecase=wsl,rocm --no-dkms
```

Reinicie a distribuição WSL a partir do PowerShell:

```powershell
wsl --shutdown
```

Abra o Ubuntu novamente e valide:

```bash
rocminfo | grep -E 'Name:|Marketing Name:'
```

O resultado precisa listar o agente `gfx1100` e `Radeon RX 7900 XTX`. Se não aparecer, pare aqui: não instale PyTorch ainda. Verifique o driver WSL do Windows, a versão WSL e a instalação do runtime.

## 3. Preparar o ambiente Python ROCm isolado

O projeto atual tem uma resolução PyTorch CPU para o perfil Hyper-V sem passthrough. Para não sobrescrever nem contaminar esse ambiente, o perfil WSL usa outro virtualenv: `.venv-wsl-rocm`.

No Ubuntu, instale o uv caso ainda não esteja disponível e entre na raiz do repositório:

```bash
sudo apt install -y curl
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
python3 --version
uv --version
```

O script abaixo executa `uv sync` nesse ambiente separado e depois substitui as wheels CPU pelas wheels ROCm 7.2 oficiais da AMD, incluindo NumPy 1.26.4 (a documentação AMD alerta para incompatibilidade dessas wheels com NumPy 2.x).

```bash
chmod +x scripts/install-wsl-rocm.sh
./scripts/install-wsl-rocm.sh
```

Para iniciar o bot usando esse ambiente sem deixar o uv sincronizar novamente as wheels CPU:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl-rocm uv run --no-sync python main.py
```

Se você executar `uv sync` nesse ambiente depois, ele poderá restaurar as wheels CPU configuradas no perfil Hyper-V. Nesse caso, rode novamente `./scripts/install-wsl-rocm.sh`. O ambiente padrão `.venv` e o perfil CPU permanecem separados.

## 4. Testar PyTorch e a GPU

```UV_PROJECT_ENVIRONMENT=.venv-wsl-rocm uv run --no-sync python - <<'PY'
import torch

print("PyTorch:", torch.__version__)
print("HIP:", torch.version.hip)
print("GPU disponível:", torch.cuda.is_available())

if not torch.cuda.is_available():
    raise SystemExit("ERRO: PyTorch não detectou a GPU ROCm")

print("GPU:", torch.cuda.get_device_name(0))
x = torch.randn((4096, 4096), device="cuda")
y = x @ x
print("Teste tensor:", y.device, tuple(y.shape))
PY
```

O namespace PyTorch continua sendo `torch.cuda` também no backend ROCm. O resultado esperado é HIP 7.2, GPU disponível e Radeon RX 7900 XTX. Este teste confirma o caminho PyTorch; não comprova automaticamente que cada plugin ou modelo do bot usa GPU.

## 5. Ligar o bot ao Ollama no Windows

Mantenha inicialmente o Ollama no Windows para o LLM. No Windows, configure o Ollama para escutar na interface de rede e crie uma regra de firewall TCP 11434 limitada ao endereço/IP da distribuição WSL. A API padrão do Ollama não autentica clientes: nunca exponha essa porta à Internet.

No Ubuntu WSL, descubra o gateway Windows:

```bash
ip route | awk '/default/ {print "Windows host/gateway:", $3; exit}'
```

Teste a API substituindo o endereço:

```bash
curl --connect-timeout 5 http://<WINDOWS_HOST_IP>:11434/api/tags
```

Em `config.yml`, use o mesmo host para:

```yaml
backend: "local"
local:
  llm:
    base_url: "http://<WINDOWS_HOST_IP>:11434/v1"
    model: "qwen3:8b"
    api_key: ""
memory:
  rag_provider: "local"
  ollama_url: "http://<WINDOWS_HOST_IP>:11434"
  ollama_embedding_model: "nomic-embed-text"
```

A rede WSL pode estar em NAT ou mirrored mode. O gateway padrão é uma pista para NAT, não um IP fixo garantido. Teste conectividade antes de alterar o arquivo de configuração.

## 6. Limitações conhecidas do perfil WSL

- **VRChat está no Windows host:** captura de tela feita por `mss` dentro do WSL não captura automaticamente a janela do VRChat no Windows. Visão do jogo exige um relay de captura Windows → bot/WSL; não declare a visão integrada como funcional até implementar e testar esse relay.
- **OSC:** `127.0.0.1` dentro do WSL é o próprio ambiente Linux. Para falar com VRChat no Windows, use o endereço Windows alcançável pela rede WSL e valide as portas UDP configuradas pelo projeto/firewall.
- **Áudio:** dispositivos de entrada/saída Windows não devem ser presumidos como dispositivos ALSA/PulseAudio no Ubuntu WSL. Teste microfone e saída separadamente.
- **GPU:** a exposição WSL não é passthrough PCIe integral. Ferramentas que exigem acesso direto ao kernel driver Linux, módulos DKMS ou recursos não suportados no WSL podem não funcionar.
- **Ollama:** esta configuração mantém Ollama no Windows inicialmente. PyTorch ROCm no WSL e aceleração de Ollama são caminhos separados.

## 7. Diagnóstico rápido

Execute no Ubuntu:

```bash
uname -a
cat /etc/os-release
rocminfo | grep -E 'gfx1100|Radeon RX 7900 XTX'
UV_PROJECT_ENVIRONMENT=.venv-wsl-rocm uv run --no-sync python -c "import torch; print(torch.__version__, torch.version.hip, torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'GPU ausente')"
```

Não considere a migração concluída até validar: ROCm detecta `gfx1100`, PyTorch executa o teste tensor na RX 7900 XTX, Ollama responde pela rede WSL e o bot inicia sem regressões. Registre os logs e erros antes de alterar plugins.

## Referências oficiais

- Matriz ROCm 7.2 para WSL: https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/compatibility/compatibilityrad/wsl/wsl_compatibility.html
- Instalação ROCm no WSL: https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/install/installrad/wsl/install-radeon.html
- PyTorch ROCm no WSL: https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/install/installrad/wsl/install-pytorch.html
- Instalação WSL Microsoft: https://learn.microsoft.com/windows/wsl/install
