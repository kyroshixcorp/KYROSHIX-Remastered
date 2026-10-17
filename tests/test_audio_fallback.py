import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audio import AudioManager


class DummyConfig:
    input_device = None
    output_device = None
    send_sample_rate = 16000
    receive_sample_rate = 24000
    chunk_size = 1024


class FakePyAudio:
    def __init__(self, devices=()):
        self.devices = devices

    def get_default_input_device_info(self):
        raise OSError("No Default Input Device Available")

    def get_default_output_device_info(self):
        raise OSError("No Default Output Device Available")

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, index):
        return self.devices[index]


def test_audio_uses_compatible_devices_when_no_default_is_set(monkeypatch):
    devices = [
        {"index": 0, "name": "Output", "maxInputChannels": 0, "maxOutputChannels": 2},
        {"index": 1, "name": "Input", "maxInputChannels": 1, "maxOutputChannels": 0},
    ]
    monkeypatch.setattr("src.audio.pyaudio.PyAudio", lambda: FakePyAudio(devices))

    audio = AudioManager(DummyConfig())

    assert audio.input_device == 1
    assert audio.output_device == 0


def test_audio_uses_silent_streams_when_no_devices_exist(monkeypatch):
    monkeypatch.setattr("src.audio.pyaudio.PyAudio", FakePyAudio)

    audio = AudioManager(DummyConfig())

    assert audio.input_device is None
    assert audio.output_device is None
    assert len(audio.open_input_stream().read(1024)) == 2048
    assert audio.open_output_stream().write(b"ABC") == 3


def test_audio_uses_silent_input_when_device_cannot_open(monkeypatch):
    class UnopenablePyAudio(FakePyAudio):
        def open(self, **kwargs):
            raise OSError(-9999, "Unanticipated host error")

    devices = [{"index": 0, "name": "Input", "maxInputChannels": 1, "maxOutputChannels": 0}]
    monkeypatch.setattr("src.audio.pyaudio.PyAudio", lambda: UnopenablePyAudio(devices))

    audio = AudioManager(DummyConfig())
    stream = audio.open_input_stream()

    assert audio.input_device is None
    assert len(stream.read(1024)) == 2048
