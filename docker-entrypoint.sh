#!/bin/sh
set -eu

plugin_source=/opt/gabriel-plugins/pocket_tts
plugin_target=/app/plugins/pocket_tts

if [ ! -f "$plugin_target/plugin.yml" ]; then
    mkdir -p /app/plugins
    cp -a "$plugin_source" "$plugin_target"
fi

exec "$@"