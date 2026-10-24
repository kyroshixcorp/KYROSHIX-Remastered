"""OSC output: movement, stop, jump."""

from __future__ import annotations

import time


# Deadzone: abaixo desse valor, não enviamos LookHorizontal para evitar
# micro-giros constantes que fazem o avatar girar no lugar (360°) sem andar.
_LOOK_DEADZONE = 0.12


class OscMixin:
    def _send_osc(self, turn, forward):
        if not self.osc or self._paused:
            return
        from src.motion_client import navigation_tick
        navigation_tick()
        client = self.osc.client
        # /input/Vertical = andar frente/trás
        client.send_message("/input/Vertical", float(max(-1, min(1, forward))))
        # /input/Horizontal = strafing esquerda/direita (0 quando só anda)
        client.send_message("/input/Horizontal", 0.0)
        # /input/LookHorizontal = rotação da câmera/cabeça.
        # Só envia quando o giro é significativo (fora do deadzone) para
        # evitar que o avatar gire 360° no lugar sem andar.
        if abs(turn) > _LOOK_DEADZONE:
            client.send_message("/input/LookHorizontal", float(max(-1, min(1, turn))))
        else:
            client.send_message("/input/LookHorizontal", 0.0)
        client.send_message("/input/Run", 0)

    def _zero_osc(self):
        if not self.osc:
            return
        client = self.osc.client
        client.send_message("/input/LookHorizontal", 0.0)
        client.send_message("/input/LookVertical", 0.0)
        client.send_message("/input/Vertical", 0.0)
        client.send_message("/input/Horizontal", 0.0)
        client.send_message("/input/Run", 0)

    def _do_jump(self):
        if not self.osc:
            return
        client = self.osc.client
        client.send_message("/input/Jump", 1)
        time.sleep(0.05)
        client.send_message("/input/Jump", 0)
