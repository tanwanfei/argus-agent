from enum import StrEnum

from argus.kernel.dialog import DialogState


class DialogEvent(StrEnum):
    SPEECH_STARTED = "speech_started"
    UTTERANCE_HEARD = "utterance_heard"
    NAME_ONLY_HEARD = "name_only_heard"
    SPEECH_DROPPED = "speech_dropped"
    REPLY_STARTED = "reply_started"
    TURN_FINISHED = "turn_finished"
    WINDOW_EXPIRED = "window_expired"
    BARGED_IN = "barged_in"
    CONVERSATION_ENDED = "conversation_ended"


class InvalidDialogTransition(Exception):
    pass


_TRANSITIONS: dict[tuple[DialogState, DialogEvent], DialogState] = {
    (DialogState.IDLE, DialogEvent.SPEECH_STARTED): DialogState.LISTENING,
    (DialogState.LISTENING, DialogEvent.UTTERANCE_HEARD): DialogState.THINKING,
    (DialogState.LISTENING, DialogEvent.NAME_ONLY_HEARD): DialogState.OPEN,
    (DialogState.LISTENING, DialogEvent.SPEECH_DROPPED): DialogState.OPEN,
    (DialogState.THINKING, DialogEvent.REPLY_STARTED): DialogState.SPEAKING,
    (DialogState.THINKING, DialogEvent.TURN_FINISHED): DialogState.OPEN,
    (DialogState.THINKING, DialogEvent.SPEECH_STARTED): DialogState.LISTENING,
    (DialogState.SPEAKING, DialogEvent.TURN_FINISHED): DialogState.OPEN,
    (DialogState.SPEAKING, DialogEvent.SPEECH_STARTED): DialogState.LISTENING,
    (DialogState.SPEAKING, DialogEvent.BARGED_IN): DialogState.OPEN,
    (DialogState.OPEN, DialogEvent.SPEECH_STARTED): DialogState.LISTENING,
    (DialogState.OPEN, DialogEvent.WINDOW_EXPIRED): DialogState.IDLE,
    (DialogState.OPEN, DialogEvent.CONVERSATION_ENDED): DialogState.IDLE,
}


class DialogStateMachine:
    def __init__(self) -> None:
        self._state = DialogState.IDLE

    @property
    def state(self) -> DialogState:
        return self._state

    def handle(self, event: DialogEvent) -> None:
        next_state = _TRANSITIONS.get((self._state, event))
        if next_state is None:
            raise InvalidDialogTransition(f"{self._state} 状态下不该来 {event}")
        self._state = next_state
