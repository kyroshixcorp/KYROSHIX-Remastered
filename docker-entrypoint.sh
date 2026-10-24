#!/bin/sh
set -eu

plugin_source=/opt/gabriel-plugins/omnivoice_cpp_tts
plugin_target=/app/plugins/omnivoice_cpp_tts

if [ ! -f "$plugin_target/plugin.yml" ]; then
    mkdir -p /app/plugins
    if [ -d "$plugin_source" ]; then
        cp -a "$plugin_source" "$plugin_target"
    fi
fi

exec "$@"
