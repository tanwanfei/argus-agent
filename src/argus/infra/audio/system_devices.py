import sounddevice as sd

from argus.infra.audio.devices import AudioDevice


def list_devices() -> list[AudioDevice]:
    host_api_names = [host_api["name"] for host_api in sd.query_hostapis()]
    return [
        AudioDevice(
            index=info["index"],
            name=info["name"],
            host_api=host_api_names[info["hostapi"]],
            input_channels=info["max_input_channels"],
            output_channels=info["max_output_channels"],
        )
        for info in sd.query_devices()
    ]


def refresh_devices() -> None:
    sd._terminate()
    sd._initialize()
