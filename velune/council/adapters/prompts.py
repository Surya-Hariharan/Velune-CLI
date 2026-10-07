"""Binds the core's ``PromptSource`` port to the committed deliberation prompt library."""

from __future__ import annotations

from velune.cognition.prompts import get_deliberation_prompt
from velune.council.domain import StageId
from velune.council.profiles import RoleProfile

SHARED_KEY = "council.general.shared"
# Stage-specific rules: the R1 rules say panelists never see each other, which is false from R2 on.
SHARED_KEYS = {
    StageId.REVIEW: "council.general.shared.review",
    StageId.REVISION: "council.general.shared.revision",
}
MODE_KEYS = {
    StageId.REVIEW: "council.general.mode.review",
    StageId.REVISION: "council.general.mode.revision",
}
LENS_PREFIX = "council.general.lens."


class LibraryPrompts:
    """Serves prompt text for one profile's seats through ``get_deliberation_prompt``.

    A seat or stage with no prompt is a loud error rather than an empty system prompt sent to a
    model. From R2 on a seat is prompted as its role's lens plus the stage's task.
    """

    def __init__(self, profile: RoleProfile) -> None:
        self._profile = profile

    def shared_prompt(self, stage: StageId) -> str:
        return get_deliberation_prompt(SHARED_KEYS.get(stage, SHARED_KEY))

    def role_prompt(self, seat_id: str, stage: StageId) -> str:
        spec = self._profile.seat(seat_id)
        if stage in MODE_KEYS:
            lens = get_deliberation_prompt(LENS_PREFIX + spec.id)
            return lens + "\n\n" + get_deliberation_prompt(MODE_KEYS[stage])
        if spec.prompt_key is None:
            raise KeyError(f"seat {seat_id!r} of profile {self._profile.id!r} has no prompt")
        return get_deliberation_prompt(spec.prompt_key)
