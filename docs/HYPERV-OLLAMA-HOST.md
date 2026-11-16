# KYROSHIX BOT — Hyper-V + Ollama no Windows 11 host

Este perfil destina-se à branch `experimental/ollama-local-ai` quando o bot roda no Linux Mint dentro do Hyper-V e a RX 7900 XTX permanece no Windows 11. **Não requer GPU passthrough.**

## Arquitetura

- Windows 11: Ollama + RX 7900 XTX (inferência LLM e embeddings).
- Linux Mint guest: KYROSHIX BOT, Python, STT local, plugins, WebUI e OSC.
- Hyper-V: rede virtual entre guest e host.
- Ollama HTTP: TCP 11434, acessível somente pela rede necessária.

A VM não enxerga a RX 7900 XTX como dispositivo ROCm. PyTorch dentro do Mint usa CPU; a inferência é enviada ao Ollama no host. A captura de tela feita dentro da VM captura a tela virtual do guest, não a janela do VRChat no Windows host.

## 1. Configurar Ollama no Windows 11

1. Instale a versão oficial do Ollama para Windows e confirme que a RX 7900 XTX aparece como suportada na documentação de GPU do Ollama.
2. Baixe os modelos no Windows:
   ```powershell
   ollama pull qwen3:8b
   ollama pull nomic-embed-text
   ollama list
   ```
3. Configure o serviço para escutar na interface de rede, não somente em loopback. No PowerShell:
   ```powershell
   setx OLLAMA_HOST "0.0.0.0:11434"
   ```
4. Encerre completamente o Ollama pela área de notificação e abra-o novamente. Se estiver executando como serviço, reinicie o serviço correspondente. Não mantenha duas instâncias simultâneas.
5. Teste localmente no Windows:
   ```powershell
   Invoke-RestMethod http://127.0.0.1:11434/api/tags
   ollama run qwen3:8b "Responda apenas: GPU TESTE"
   ollama ps
   ```
   Durante a inferência, confira a coluna `PROCESSOR` em `ollama ps`. Se não indicar GPU, resolva primeiro o driver AMD ROCm v7/HIP7 ou o backend gráfico compatível no Windows; a conectividade da VM não corrige aceleração ausente.

> Segurança: a API padrão do Ollama não exige autenticação. Não encaminhe a porta 11434 para a Internet nem crie regra de firewall aberta para qualquer origem.

## 2. Descobrir o IP do host pelo Linux Mint

No terminal do guest:
```bash
ip route
```
Identifique o endereço após `default via`. Em muitas configurações do Default Switch do Hyper-V, esse gateway é o endereço do host visível pelo guest. Não presuma que será sempre o mesmo: confirme com:
```bash
HOST_IP="$(ip route | awk '/^default/ {print $3; exit}')"
printf 'Windows host: %s\n' "$HOST_IP"
curl --connect-timeout 5 "http://$HOST_IP:11434/api/tags"
```

Se a VM usa um switch virtual External, Internal ou NAT personalizado, use o IP do adaptador virtual do Windows que está na mesma rede do guest, não necessariamente o gateway padrão.

## 3. Restringir o Firewall do Windows

Primeiro obtenha o IPv4 atual do guest com `ip -4 addr` no Mint. No PowerShell elevado do Windows, crie uma regra limitada ao IP do guest (substitua o endereço):
```powershell
New-NetFirewallRule -DisplayName "KYROSHIX Ollama from Hyper-V guest" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11434 -RemoteAddress 172.28.224.10 -Profile Any
```

Substitua `172.28.224.10` pelo IPv4 real da VM. Se o IP do guest mudar por DHCP, atualize a regra ou configure uma reserva DHCP no switch/rede que você controla. Não use `Any` como origem e não exponha a porta no roteador.

## 4. Configurar o bot no Mint

Faça backup antes de editar:
```bash
cp config.yml config.yml.backup
```

Em `config.yml`, configure os dois endereços para o mesmo IP do Windows host:

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

Troque literalmente `<WINDOWS_HOST_IP>` pelo IP descoberto no passo 2. Não use os sinais `<>` no arquivo final. O endpoint LLM usa o sufixo `/v1`; o endpoint de embeddings usa a raiz da API Ollama.

Teste antes de iniciar o bot:
```bash
HOST_IP="$(ip route | awk '/^default/ {print $3; exit}')"
curl -fsS "http://$HOST_IP:11434/api/tags"
curl -fsS "http://$HOST_IP:11434/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"Responda apenas: KYROSHIX OK"}],"stream":false}'
```

## 5. Dependências Python no guest

Como não há passthrough, o guest não deve instalar builds ROCm/CUDA do PyTorch para este perfil. O `pyproject.toml` desta branch aponta `torch`, `torchvision` e `torchaudio` para wheels CPU. O processamento LLM e embeddings ocorre no Windows via Ollama; tarefas locais como STT/YOLO continuam na CPU da VM, salvo aceleração separada configurada no guest.

Instalação com uv:
```bash
uv sync
```

Não execute o compose ROCm `docker-compose.amd-rocm.yml` dentro desta VM: ele pressupõe dispositivos GPU Linux (`/dev/kfd`, `/dev/dri`) que não são fornecidos sem passthrough.

## 6. VRChat OSC e visão

- Se o VRChat continua no Windows host, `127.0.0.1` dentro do Mint aponta para a VM, não para o Windows.
- Para OSC, configure o IP do Windows host como destino no bot e permita UDP 9000 no firewall do Windows, restrito ao IP do guest. Para receber OSC de volta em UDP 9001, configure o caminho inverso e a regra correspondente.
- A captura de tela no guest não vê automaticamente a janela VRChat do host. Para visão do VRChat, será necessário um relay de captura no Windows que envie frames ao guest, ou executar o cliente VRChat no próprio ambiente que faz a captura.

## 7. Diagnóstico

No Mint:
```bash
ip route
ip -4 addr
curl -v --connect-timeout 5 http://<WINDOWS_HOST_IP>:11434/api/tags
```

No Windows:
```powershell
Get-NetTCPConnection -LocalPort 11434 -State Listen
Get-NetFirewallRule -DisplayName "KYROSHIX Ollama from Hyper-V guest"
```

Se a conexão falhar, verifique nesta ordem: Ollama reiniciado após definir `OLLAMA_HOST`; endereço IP do host correto; regra de firewall com IP atual do guest; perfil de rede do adaptador; switch virtual do Hyper-V.

## Limites e segurança

- O endpoint Ollama não possui autenticação por padrão; mantenha a porta restrita à rede virtual confiável.
- A VM de 12 GB usa Qwen3 8B como perfil inicial, mas a memória disponível para STT, YOLO e contexto deve ser monitorada.
- A RX 7900 XTX permanece dedicada ao Windows e ao Ollama; o guest não acessa VRAM diretamente.
- TTS Qwen3 continua sendo um serviço separado. O endpoint TTS configurado em `tts.local_openai.base_url` deve apontar para onde o servidor TTS realmente roda (por padrão, localhost do guest).
