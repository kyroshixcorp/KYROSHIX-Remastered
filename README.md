# KYROSHIX-Remastered — VRChat AI Bot (AMD ROCm + Docker 24/7)

Bot de VRChat com memória persistente, inteligência avançada, GPU AMD RX 7900 XTX via ROCm, e WebUI de controle. Roda 24/7 em containers Docker.

---

## Arquitetura

| Componente | Descrição |
|---|---|
| `gabriel` | Bot principal (Python, Gemini Live, ROCm/PyTorch) |
| `social-server` | Servidor de mensagens AI-to-AI (Node.js) |
| WebUI | Painel de controle em `http://localhost:8766` |
| Memória | SQLite persistente + RAG local (ChromaDB + LM Studio) |
| Áudio | TTS com cache LRU, noise gate, limiter soft-knee |

---

## Requisitos

- Linux Debian-based (Kodachios, Ubuntu, etc.)
- GPU AMD RX 7900 XTX (gfx1100) com drivers ROCm instalados
- Docker + Docker Compose
- Chave Gemini API (gratuita em https://aistudio.google.com/apikey)

---

## Instalação (host)

```bash
# 1. Instale dependências do sistema (senha: admin)
sudo apt-get update
sudo apt-get install -y portaudio19-dev ffmpeg docker.io docker-compose

# 2. Adicione seu usuário ao grupo docker
sudo usermod -aG docker $USER
# (faça logout e login novamente)

# 3. Adicione seu usuário aos grupos video/render (acesso à GPU)
sudo usermod -aG video $USER
sudo usermod -aG render $USER

# 4. Clone/entre no repositório
cd /home/kyroshix/KYROSHIX-Remastered
```

---

## Configuração

### 1. Chave Gemini

Edite `config.yml` e defina sua chave:

```yaml
gemini:
  api_key: "SUA_CHAVE_GEMINI_AQUI"
```

Ou defina como variável de ambiente:

```bash
export GABRIEL_GEMINI_API_KEY="SUA_CHAVE_AQUI"
```

### 2. config.yml — ajustes recomendados

```yaml
backend: "gemini_live"
memory:
  enabled: true
  backend: "sqlite"
  rag_enabled: true
  rag_provider: "local"   # totalmente offline
backend: "gemini_live"
```

### 3. Credenciais WebUI

São geradas automaticamente na primeira execução:

- Usuário: `admin`
- Senha: `#0aIH8ue-hsO`

Armazenado em `data/web_auth.env`.

---

## Execução com Docker (ROCm + AMD GPU)

```bash
cd /home/kyroshix/KYROSHIX-Remastered

# Build e suba os containers
sudo docker compose -f docker-compose.amd-rocm.yml up -d

# Veja os logs
sudo docker compose -f docker-compose.amd-rocm.yml logs -f gabriel

# Pare
sudo docker compose -f docker-compose.amd-rocm.yml down
```

O container `gabriel` expõe:
- `/dev/kfd` e `/dev/dri` → acesso direto à GPU AMD
- `restart: unless-stopped` → reinicia automaticamente se travar (24/7)

---

## Execução local (sem VRChat, para testar)

```bash
cd /home/kyroshix/KYROSHIX-Remastered

# Instale dependências Python
pip install --break-system-packages -r requirements.txt
pip install --break-system-packages pyaudio torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install --break-system-packages google-genai python-osc pyyaml pedalboard opencv-python ultralytics bettercam mss aiohttp beautifulsoup4 Pillow scipy psutil colorama discord.py-self fastapi uvicorn python-multipart pytz nltk websockets chromadb transformers

# Configure a chave Gemini
export GABRIEL_GEMINI_API_KEY="SUA_CHAVE_AQUI"

# Rode
python3 main.py
```

Acesse: `http://localhost:8766/login` (admin / `#0aIH8ue-hsO`)

---

## WebUI — Painel de Controle

Acesse `http://localhost:8766/login`

### Abas disponíveis

| Aba | O que faz |
|---|---|
| Dashboard | Console ao vivo, status de conexão, mudo |
| Memories | Visualizar, buscar e gerenciar memórias persistentes |
| Music | Controle de reprodução de música |
| Players | Lista de jogadores no mundo VRChat (via OSC) |
| Mapping | Mapeamento de mundos e waypoints |
| Waypoints | Pontos de navegação autônoma |
| Vision | Captura de tela para visão do AI |
| **System Health** | RAM, GPU, uptime, sessão, watchdog, métricas |

### Endpoints da API

| Endpoint | Método | Descrição |
|---|---|---|
| `/api/health` | GET | Saúde do sistema (RAM, GPU, uptime, sessão) |
| `/metrics` | GET | Métricas Prometheus |
| `/api/audio-devices` | GET | Lista dispositivos de áudio |
| `/api/audio-device` | POST | Define dispositivo ativo |
| `/api/tts-cache` | GET | Estatísticas do cache de áudio TTS |
| `/api/tts-cache/clear` | POST | Limpa cache TTS |
| `/api/memory/maintenance` | POST | Executa manutenção da memória |
| `/api/memory/intelligence` | GET | Estatísticas de inteligência da memória |
| `/api/session/reconnect` | POST | Força reconexão da sessão Gemini |
| `/api/circuits` | GET | Status dos circuit breakers |
| `/api/circuits/{service}/reset` | POST | Reseta circuit breaker |

---

## Serviço systemd (24/7 automático)

```bash
sudo cp kyroshix-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kyroshix-bot
sudo systemctl status kyroshix-bot
```

---

## Solução de problemas

### Docker não consegue instalar dependências (sem rede dentro do container)

O `Dockerfile.rocm` usa `set +eux` para apt-get — se não houver rede, o build continua e o bot roda com o que já está na imagem `rocm/pytorch`. Para áudio completo, garanta que `portaudio19-dev` esteja instalado no host.

### Avatar gira 360° no lugar (não anda)

Foi corrigido em `src/vrchat.py` (`set_movement` agora usa `/input/Horizontal`) e `src/wanderer/osc_out.py` (deadzone de 0.12 no `LookHorizontal`). Reconstrua o container após a correção.

### Memória RAM crescendo sem parar

Foram adicionados limites: logger limitado a 500 entradas, áudio debug a 64 MB, filas com tamanho máximo, `gc.collect()` periódico, e vakuum do SQLite a cada 10 min.

### Chave Gemini esgotando (quota)

Há rotação automática de chaves (backup_keys) e circuit breaker que abre após 5 falhas consecutivas, fechando após 60s.

---

## Estrutura de pastas

```
KYROSHIX-Remastered/
├── config.yml              # Configuração principal
├── config.yml.example      # Exemplo de config
├── Dockerfile.rocm         # Imagem Docker com ROCm
├── docker-compose.amd-rocm.yml  # Compose com suporte a GPU AMD
├── kyroshix-bot.service    # Serviço systemd 24/7
├── data/                   # Dados persistentes (memória, logs, auth)
├── src/                    # Código Python do bot
├── webui-src/              # Código-fonte React do WebUI
├── models/                 # Modelos YOLO, etc.
├── sfx/                    # Efeitos sonoros
└── plugins/                # Plugins drop-in
```

---

## Licença

Uso pessoal / educacional.
