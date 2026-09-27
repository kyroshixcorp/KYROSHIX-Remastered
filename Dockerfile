FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PULSE_SERVER=unix:/run/pulse/native \
    SDL_AUDIODRIVER=pulse \
    HF_HOME=/app/data/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ffmpeg \
        git \
        libasound2-plugins \
        libgl1 \
        libglib2.0-0 \
        libpulse0 \
        libsndfile1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

RUN printf '%s\n' 'pcm.!default { type pulse }' 'ctl.!default { type pulse }' > /etc/asound.conf

ENV UV_INSTALL_DIR=/usr/local/bin
ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh

WORKDIR /app
COPY pyproject.toml ./
RUN uv sync --no-dev --extra local --no-install-project

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && git clone --depth 1 https://github.com/HoppouAI/ProjectGabriel-Plugins.git /tmp/gabriel-plugins \
    && mkdir -p /opt/gabriel-plugins \
    && cp -a /tmp/gabriel-plugins/pocket_tts /opt/gabriel-plugins/ \
    && rm -rf /tmp/gabriel-plugins

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uv", "run", "--no-sync", "python", "main.py"]