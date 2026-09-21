"""KYROSHIX BOT - VRChat direct autojoin."""

from __future__ import annotations

import os
import subprocess
import sys
import time

WORLD_ID = "wrld_7d6e45ce-d03e-4d98-9b11-0ac33239a3cc"

INSTANCE_ID = (
    "74534"
    "~group(grp_bdcf1844-c9f3-4e97-bdee-de1e329f1d00)"
    "~groupAccessType(public)"
    "~region(use)"
)

LAUNCH_DELAY_SECONDS = 8


def vrchat_is_running() -> bool:
    """Verifica se o VRChat já está aberto no Windows."""
    if sys.platform != "win32":
        return False

    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq VRChat.exe"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        return "VRChat.exe" in result.stdout

    except Exception as exc:
        print(f"[KYROSHIX] Não foi possível verificar o VRChat: {exc}")
        return False


def build_uri() -> str:
    return f"vrchat://launch?id={WORLD_ID}:{INSTANCE_ID}"


def launch() -> int:
    if sys.platform != "win32":
        print("[KYROSHIX] Autojoin configurado somente para Windows.")
        return 1

    print("[KYROSHIX] ========================================")
    print("[KYROSHIX] VRChat Autojoin")
    print(f"[KYROSHIX] World: {WORLD_ID}")
    print("[KYROSHIX] Instance: Cruzeiro Brasil / Group Public")

    # Não tenta abrir outra instância se VRChat já estiver executando.
    if vrchat_is_running():
        print("[KYROSHIX] VRChat já está aberto.")
        print("[KYROSHIX] Usando a instância atual.")
        print("[KYROSHIX] Autojoin ignorado para evitar abrir outro VRChat.")
        print("[KYROSHIX] ========================================")
        return 0

    uri = build_uri()

    print("[KYROSHIX] VRChat não está aberto.")
    print("[KYROSHIX] Iniciando VRChat diretamente no mapa...")
    print("[KYROSHIX] ========================================")

    try:
        os.startfile(uri)

    except OSError as exc:
        print("[KYROSHIX] Windows não conseguiu abrir vrchat://")
        print(f"[KYROSHIX] Erro: {exc}")
        print("[KYROSHIX] Tentando através do comando START...")

        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", uri],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        except Exception as fallback_exc:
            print(f"[KYROSHIX] Autojoin falhou: {fallback_exc}")
            return 1

    time.sleep(LAUNCH_DELAY_SECONDS)

    print("[KYROSHIX] Comando enviado ao VRChat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(launch())