from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

WASAPI = "Windows WASAPI"

type Direction = Literal["input", "output"]


class AudioDeviceError(Exception):
    pass


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    input_channels: int
    output_channels: int


def find_wasapi_device(
    devices: Iterable[AudioDevice], name: str, direction: Direction
) -> AudioDevice:
    candidates = [
        device
        for device in devices
        if device.host_api == WASAPI and _channel_count(device, direction) > 0
    ]
    matches = [device for device in candidates if device.name == name]
    if len(matches) == 1:
        return matches[0]

    label = "收音" if direction == "input" else "放音"
    if matches:
        problem = f"有 {len(matches)} 个{label}设备都叫「{name}」，分不出该用哪个"
    else:
        problem = f"找不到{label}设备「{name}」：检查设备是否插好、设备名是否写对"
    available = "\n".join(f"  {device.name}" for device in candidates) or "  （一个都没有）"
    raise AudioDeviceError(f"{problem}\n现有的{label}设备：\n{available}")


def take_channel(interleaved_pcm: bytes, channel: int, channel_count: int) -> bytes:
    samples = array("h", interleaved_pcm)
    return samples[channel::channel_count].tobytes()


def duplicate_channel(mono_pcm: bytes, channel_count: int) -> bytes:
    mono_samples = array("h", mono_pcm)
    interleaved = array("h", bytes(len(mono_pcm) * channel_count))
    for channel in range(channel_count):
        interleaved[channel::channel_count] = mono_samples
    return interleaved.tobytes()


def _channel_count(device: AudioDevice, direction: Direction) -> int:
    return device.input_channels if direction == "input" else device.output_channels
