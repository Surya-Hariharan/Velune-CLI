from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.hardware.profiles import RuntimeProfile


class SessionMode(Enum):
    NORMAL = "normal"
    OPTIMUS = "optimus"
    GODLY = "godly"


@dataclass
class ModeConfig:
    mode: SessionMode
    context_compression: bool  # compress context before each call
    max_context_tokens: int  # hard cap on context sent per call
    temperature: float
    retrieval_depth: int  # how many memory + repo chunks to pull
    use_fastest_model: bool  # optimus: pick smallest capable model
    use_largest_model: bool  # godly: pick largest available model
    description: str
    prompt_color: str  # Rich color for prompt badge


MODE_CONFIGS: dict[SessionMode, ModeConfig] = {
    SessionMode.NORMAL: ModeConfig(
        mode=SessionMode.NORMAL,
        context_compression=False,
        max_context_tokens=16384,
        temperature=0.3,
        retrieval_depth=8,
        use_fastest_model=False,
        use_largest_model=False,
        description="Balanced — model, context and retrieval follow your hardware",
        prompt_color="cyan",
    ),
    SessionMode.OPTIMUS: ModeConfig(
        mode=SessionMode.OPTIMUS,
        context_compression=True,
        max_context_tokens=4096,
        temperature=0.1,
        retrieval_depth=3,
        use_fastest_model=True,
        use_largest_model=False,
        description="Speed mode — smallest model, compressed context",
        prompt_color="yellow",
    ),
    SessionMode.GODLY: ModeConfig(
        mode=SessionMode.GODLY,
        context_compression=False,
        max_context_tokens=128000,
        temperature=0.5,
        retrieval_depth=20,
        use_fastest_model=False,
        use_largest_model=True,
        description="Maximum — largest model, full context",
        prompt_color="magenta",
    ),
}


class ModeManager:
    def __init__(self, runtime_profile: RuntimeProfile | None = None) -> None:
        self._active = SessionMode.NORMAL
        self._profile = runtime_profile

    @property
    def current(self) -> SessionMode:
        return self._active

    @property
    def runtime_profile(self) -> RuntimeProfile | None:
        return self._profile

    @property
    def config(self) -> ModeConfig:
        config = MODE_CONFIGS[self._active]
        # NORMAL mode adapts to the hardware-derived runtime profile; explicit
        # user modes (/mode fast, /mode max) always keep their fixed settings.
        if self._active == SessionMode.NORMAL and self._profile is not None:
            return replace(
                config,
                max_context_tokens=self._profile.max_context_tokens,
                retrieval_depth=self._profile.retrieval_depth,
                context_compression=self._profile.context_compression,
                description=f"{config.description} · profile: {self._profile.label}",
            )
        return config

    def set_mode(self, mode: SessionMode) -> ModeConfig:
        self._active = mode
        return self.config

    def is_normal(self) -> bool:
        return self._active == SessionMode.NORMAL
