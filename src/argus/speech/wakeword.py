from typing import Protocol

from argus.channels.audio import AudioFrame


class WakeWord(Protocol):
    def feed(self, frame: AudioFrame) -> bool: ...
