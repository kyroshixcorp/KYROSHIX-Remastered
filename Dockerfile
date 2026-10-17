FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PULSE_SERVER=unix:/run/pulse/native \
    SDL_AUDIODRIVER=pulse \
    HF_HOME=/app/data/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
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
        glslc \
        libvulkan-dev \
        libvulkan1 \
        mesa-vulkan-drivers \
        ninja-build \
        portaudio19-dev \
        vulkan-tools \
    && rm -rf /var/lib/apt/lists/*

RUN printf '%s\n' 'pcm.!default { type pulse }' 'ctl.!default { type pulse }' > /etc/asound.conf

ENV UV_INSTALL_DIR=/usr/local/bin
ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh

ENV OMNIVOICE_CPP_DIR=/opt/omnivoice_cpp_tts/native \
    LD_LIBRARY_PATH=/opt/omnivoice_cpp_tts/native:/usr/lib/wsl/lib

WORKDIR /app
COPY pyproject.toml ./
RUN uv sync --no-dev --extra local --no-install-project

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && git clone --depth 1 --recurse-submodules https://github.com/ServeurpersoCom/omnivoice.cpp.git /tmp/omnivoice.cpp \
    && cmake -S /tmp/omnivoice.cpp -B /tmp/omnivoice.cpp/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DOMNIVOICE_SHARED=ON \
    && cmake --build /tmp/omnivoice.cpp/build --parallel 2 \
    && mkdir -p /opt/omnivoice_cpp_tts/native \
    && find /tmp/omnivoice.cpp/build -type f -name '*.so*' \
        -exec cp -a {} /opt/omnivoice_cpp_tts/native/ \; \
    && test -f /opt/omnivoice_cpp_tts/native/libomnivoice.so \
    && git clone --depth 1 https://github.com/HoppouAI/ProjectGabriel-Plugins.git /tmp/gabriel-plugins \
    && mkdir -p /opt/gabriel-plugins \
    && cp -a /tmp/gabriel-plugins/omnivoice_cpp_tts /opt/gabriel-plugins/ \
    && rm -rf /tmp/gabriel-plugins /tmp/omnivoice.cpp

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uv", "run", "--no-sync", "python", "main.py"]