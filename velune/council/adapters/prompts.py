"""Binds the core's ``PromptSource`` port to the committed deliberation prompt library."""

from __future__ import annotations

from velune.cognition.prompts import get_deliberation_prompt
from velune.council.domain import StageId
from velune.council.profiles import RoleProfile

SHARED_KEY = "council.general.shared"


class LibraryPrompts:
    """Serves prompt text for one profile's seats through ``get_deliberation_prompt``.

    A seat whose ``prompt_key`` is unset has no prompt; asking for it is a loud error rather than
    an empty system prompt sent to a model.
    """

    def __init__(self, profile: RoleProfile) -> None:
        self._profile = profile

    def shared_prompt(self) -> str:
        return get_deliberation_prompt(SHARED_KEY)

    def role_prompt(self, seat_id: str, stage: StageId) -> str:
        key = self._profile.seat(seat_id).prompt_key
        if key is None:
            raise KeyError(f"seat {seat_id!r} of profile {self._profile.id!r} has no prompt")
        return get_deliberation_prompt(key)
