# KYROSHIX BOT — Linux Mint VM on Windows 11

Este guia prepara a execução do bot dentro de uma VM Linux Mint, mantendo o backend de IA local. Ele **não** presume um hypervisor nem configura passthrough automaticamente.

## Estado e segurança

- Trabalhe somente na branch `experimental/ollama-local-ai`.
- A branch `main` e a publicação de produção não fazem parte deste procedimento.
- Faça backup de `config.yml`, `data/`, `models/`, `plugins/` e `sfx/` antes de migrar.
- Não copie arquivos de autenticação, tokens ou chaves para commits.
- O perfil Ollama não precisa de chave Gemini. Deixe `gemini.api_key` vazio e não exporte `GABRIEL_GEMINI_API_KEY`.

## Atenção: passthrough e uso da GPU pelo Windows

A RX 7900 XTX é uma GPU PCIe dedicada. Passthrough PCI integral normalmente entrega o dispositivo ao guest e deixa de disponibilizá-lo ao Windows host enquanto a VM estiver ligada. Não é o mesmo que aceleração 3D virtual ou GPU paravirtualizada.

**Há um conflito se o VRChat continuar rodando no Windows host usando essa mesma RX 7900 XTX.** Para usar a GPU dentro da VM e manter VRChat no Windows ao mesmo tempo, será necessário um segundo adaptador gráfico utilizável pelo host, ou mudar a arquitetura (por exemplo, manter VRChat no host e executar Ollama no host/CPU, ou executar VRChat também no guest). O processador Xeon E5-2699 v3 não possui iGPU integrada.

Antes de qualquer alteração no BIOS, driver ou VM, identifique o hypervisor exato. VMware Workstation, VirtualBox, Hyper-V e soluções baseadas em QEMU/KVM não oferecem o mesmo tipo de acesso PCI. Não aplique comandos VFIO de Linux no Windows host.

## Compatibilidade AMD / Linux Mint

A RX 7900 XTX é RDNA 3, alvo `gfx1100`, listada como GPU suportada pelo ROCm. Entretanto, a matriz oficial AMD ROCm lista distribuições Ubuntu específicas, Debian e RHEL; Linux Mint não aparece como sistema oficialmente validado. O fato de Mint usar uma base Ubuntu não garante suporte oficial ao instalador ROCm.

Plano de validação:
1. Comece com a versão Mint instalada e registre kernel/base Ubuntu.
2. Só instale ROCm depois que o guest detectar a RX 7900 XTX e os dispositivos `/dev/kfd` e `/dev/dri`.
3. Valide `rocminfo` e PyTorch antes de ativar qualquer componente GPU do bot.
4. Se ROCm falhar por incompatibilidade da distribuição, use Ollama com backend Vulkan se o driver Vulkan do guest funcionar, ou migre o guest para uma versão Ubuntu explicitamente suportada pela matriz AMD.
5. Não defina `HSA_OVERRIDE_GFX_VERSION` para RX 7900 XTX: o alvo nativo é `gfx1100`; override não é correção para passthrough ausente.

## Recursos da VM

Configuração inicial sugerida, sujeita ao hypervisor:
- RAM: 12 GB (conforme solicitado).
- CPU: 6–8 vCPU inicialmente; não atribua todos os threads do host.
- Disco: 100 GB dinâmicos no mínimo, preferível 150 GB para modelos, cache de áudio e memória.
- Rede: adaptador bridged quando disponível, para comunicação direta entre guest e Windows host.
- GPU: passthrough PCI integral somente após confirmar que o host terá outro adaptador de vídeo ou aceitará ficar sem a RX 7900 XTX.

Com 12 GB de RAM do guest, comece com `qwen3:8b` e `nomic-embed-text`. A VRAM de 24 GB da placa pode permitir modelos maiores, mas o tamanho do modelo, contexto, KV cache e memória do sistema precisam ser medidos. Não presuma que `qwen3:14b` ficará estável só por caber na VRAM.

## 1. Diagnóstico do guest Mint

Na VM, clone a branch experimental e rode o diagnóstico:

```bash
git clone --branch experimental/ollama-local-ai --single-branch https://github.com/kyroshixcorp-sudo/KYROSHIX-Remastered.git
cd KYROSHIX-Remastered
chmod +x scripts/diagnose-linux-vm.sh
./scripts/diagnose-linux-vm.sh
```

O script é somente leitura. Ele não instala pacotes, não altera configurações e não inicia serviços. Guarde a saída para validar GPU, kernel, rede e Ollama.

Critérios para prosseguir com aceleração AMD:
- `lspci` mostra a RX 7900 XTX dentro do guest.
- `/dev/dri` existe; para ROCm também deve existir `/dev/kfd`.
- O usuário pertence aos grupos `render` e `video` (quando esses grupos existirem).
- `rocminfo` lista o agente gfx1100, ou `vulkaninfo --summary` lista a GPU AMD se usar Vulkan.

## 2. Dependências base do Linux Mint

Depois de validar a VM e criar um snapshot:

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential cmake   ffmpeg portaudio19-dev libasound2-dev libsndfile1 libgl1   libglib2.0-0 libxext6 libxrender1 libsm6 vulkan-tools pciutils
```

Use Python 3.11 ou 3.12, conforme `pyproject.toml`. Não use `pip --break-system-packages`; crie um ambiente virtual com `uv` ou `venv`.

## 3. Ollama dentro do guest

Instale Ollama no próprio Linux Mint (não no Windows host) para que `127.0.0.1` continue apontando para o serviço local do guest:

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
ollama pull qwen3:8b
ollama pull nomic-embed-text
curl http://127.0.0.1:11434/api/tags
```

O Ollama documenta suporte ROCm para RX 7900 XTX em Linux com stack ROCm compatível. Vulkan é uma alternativa possível quando ROCm não está disponível, mas precisa ser testada no guest. Não exponha a API Ollama em `0.0.0.0` sem firewall e autenticação/isolamento de rede.

Teste a API OpenAI-compatible usada pelo bot:

```bash
curl http://127.0.0.1:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"Responda: KYROSHIX LOCAL OK"}]}'
```

## 4. Configuração do bot

Crie a configuração a partir do exemplo e mantenha os dados existentes:

```bash
cp -n config.yml.example config.yml
```

No `config.yml`, confirme os valores que já existem no exemplo desta branch:
- `backend: "local"`
- `local.llm.base_url: "http://127.0.0.1:11434/v1"`
- `local.llm.model: "qwen3:8b"`
- `local.llm.api_key: ""`
- `memory.rag_provider: "local"`
- `memory.ollama_url: "http://127.0.0.1:11434"`
- `memory.ollama_embedding_model: "nomic-embed-text"`

O provider `qwen3_local` de TTS depende de um servidor Qwen3-TTS externo ao processo do bot, mas ainda local na VM. O contrato atual espera `POST /v1/audio/speech/stream` e PCM int16 mono a 24 kHz. Não considere TTS pronto até testar esse endpoint real. STT `faster-whisper` pode começar em CPU com modelo `small`, `int8`.

## 5. VRChat no Windows host: rede e OSC

Se o VRChat permanecer no Windows host:
- A VM precisa alcançar o IP LAN do Windows host.
- Configure `vrchat.osc_ip` para o IP do Windows host, não `127.0.0.1`.
- O bot envia OSC para UDP 9000 no host; crie uma regra de firewall Windows restrita ao IP da VM.
- O recebimento de parâmetros OSC do VRChat para UDP 9001 na VM exige configurar/implementar um relay UDP no Windows host ou outra rota OSC. O endereço loopback `127.0.0.1` do VRChat não atravessa a VM automaticamente.
- A captura de tela feita dentro da VM não captura a janela do VRChat no Windows host. Para o recurso Vision, será necessário rodar VRChat no guest ou implementar um fluxo de captura/stream do host para a VM.

Não altere `osc_ip` nem abra portas antes de identificar o modo de rede (NAT, bridged ou host-only) e os endereços IP reais.

## 6. Execução e testes

Primeiro teste sem Docker para isolar os problemas de GPU, áudio e rede:

```bash
uv sync --extra local
uv run python main.py
```

Se usar `venv`, instale as dependências de runtime dentro dele e selecione explicitamente a build PyTorch apropriada. **A configuração atual de `pyproject.toml` desta branch declara um índice PyTorch CUDA 12.6; não execute `uv sync` como se essa seleção já fosse adequada para AMD/ROCm.** A seleção de wheels PyTorch ROCm deve ser resolvida para a versão ROCm efetivamente instalada no guest antes do teste de Tracker/Vision.

Valide separadamente:
1. Ollama responde e usa a GPU (logs do Ollama).
2. STT transcreve um arquivo local.
3. TTS responde no endpoint PCM documentado.
4. WebUI abre em `http://127.0.0.1:8766/login`.
5. OSC de saída chega ao Windows host.
6. OSC de entrada chega ao bot via relay.
7. Vision captura a origem correta (VM ou relay do Windows).

## Informações necessárias antes do passthrough

Para gerar o procedimento específico, ainda são necessários:
- Nome e versão exatos do hypervisor (o valor "Outro" não identifica a plataforma).
- Versão exata do Linux Mint (ex.: 22.1/22.2/22.3).
- Se o Windows host precisa continuar usando a RX 7900 XTX para VRChat enquanto a VM estiver ligada.
- Se existe uma segunda GPU para manter a saída de vídeo do Windows host.

Não habilite IOMMU, altere o driver da RX 7900 XTX, remova o dispositivo do Windows ou edite o bootloader com base em instruções genéricas.
