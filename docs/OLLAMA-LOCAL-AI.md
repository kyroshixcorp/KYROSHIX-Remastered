# KYROSHIX BOT — migração para 100% Local AI (Ollama)

Esta branch prepara o backend local como padrão. A branch `main` não é alterada.

## Arquitetura local

- **LLM:** Ollama pela API OpenAI-compatible em `http://127.0.0.1:11434/v1`.
- **Modelo inicial:** `qwen3:14b`.
- **STT:** mantém o pipeline local existente (faster-whisper ou Parakeet).
- **TTS:** provider `qwen3_local`, sem Authorization/API key. Espera um wrapper Qwen3-TTS local com `POST /v1/audio/speech/stream`, que devolva PCM int16 mono a 24 kHz.
- **Memória:** SQLite + ChromaDB; embeddings via Ollama com `nomic-embed-text`.
- **VRChat:** mantém OSC, ferramentas, plugins, chatbox, tracker, wanderer, WebUI e os demais subsistemas existentes.

## Preparação no Ryoku Linux

1. Confirme o serviço Ollama:
   ```bash
   systemctl --user status ollama
   curl http://127.0.0.1:11434/api/tags
   ```
   Se estiver instalado como serviço de sistema, use `sudo systemctl status ollama`.

2. Baixe os modelos:
   ```bash
   ollama pull qwen3:14b
   ollama pull nomic-embed-text
   ```

3. Faça backup do `config.yml` atual e configure os valores equivalentes ao `config.yml.example` desta branch:
   - `backend: "local"`
   - `local.llm.base_url: "http://127.0.0.1:11434/v1"`
   - `local.llm.model: "qwen3:14b"`
   - `local.llm.api_key: ""`
   - `tts.provider: "qwen3_local"`
   - `tts.local_openai.base_url`: endereço real do servidor Qwen3-TTS local.

4. Teste o LLM sem iniciar o bot:
   ```bash
   curl http://127.0.0.1:11434/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"qwen3:14b","messages":[{"role":"user","content":"Responda apenas: Ollama OK"}]}'
   ```

5. O servidor Qwen3-TTS precisa implementar a rota e o formato de áudio acima. Se o seu clone usa outra porta, rota, payload ou entrega WAV/MP3, será necessário ajustar o adaptador antes do teste de voz.

## Recursos preservados

O backend local existente continua usando a sessão de ferramentas e o mesmo orquestrador VRChat. Permanecem o histórico conversacional da sessão, tool calling, personalidade, memória persistente SQLite, chatbox, OSC, plugins, áudio, rastreamento e WebUI. A captura de visão continua disponível quando o modelo configurado for multimodal (por exemplo, Qwen3-VL); o modelo textual `qwen3:14b` não interpreta imagens.

## Sem API keys não significa contexto infinito

Chamadas locais ao Ollama não exigem chave nem cobrança por token. Ainda existem limites técnicos: janela de contexto do modelo, limite de saída, VRAM/RAM e latência. Contextos maiores aumentam o consumo de memória.

## Proteção dos dados

- Não apague `data/`, `config/` ou `plugins/`.
- Preserve o banco SQLite no caminho atual de `memory.sqlite_path`.
- Faça backup de `config.yml` antes de copiar as configurações novas.
- A chave Gemini fica vazia no perfil local e não é enviada ao Ollama.
- Gemini Live e RAG Gemini permanecem no código para seleção manual, mas não são usados pelo perfil local.

## Validação pendente

A integração de TTS foi preparada para um servidor Qwen3-TTS compatível com o contrato acima. Ela ainda precisa ser testada contra o endpoint exato instalado no computador. Até esse teste passar, considere o LLM e a memória local preparados, mas não declare a saída de voz validada.
