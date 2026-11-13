from enum import StrEnum

from argus.delivery.outbound import Verdict


class Exit(StrEnum):
    APP = "app"
    SPEAKER = "speaker"
    BARK = "bark"


def choose_exit(
    verdict: Verdict,
    *,
    app_voice_online: bool,
    at_home: bool,
    speaker_online: bool,
    personal: bool,
    others_present: bool,
) -> Exit:
    match verdict:
        case Verdict.SPEAK:
            if app_voice_online:
                return Exit.APP
            if at_home and speaker_online and not (personal and others_present):
                return Exit.SPEAKER
            return Exit.BARK
        case Verdict.NOTIFY:
            return Exit.BARK
        case Verdict.DIGEST | Verdict.DROP:
            raise ValueError(f"判成 {verdict.value} 的消息不该交给 delivery")
