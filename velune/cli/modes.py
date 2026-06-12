from dataclasses import dataclass
from enum import Enum


class SessionMode(Enum):
    NORMAL = "normal"
    OPTIMUS = "optimus"
    GODLY = "godly"


@dataclass
class ModeConfig:
    mode: SessionMode
    council_tier: str  # "instant" | "minimal" | "standard" | "full" | "auto"
    context_compression: bool  # compress context before each call
    max_context_tokens: int  # hard cap on context sent per call
    temperature: float
    retrieval_depth: int  # how many memory + repo chunks to pull
    use_fastest_model: bool  # optimus: pick smallest capable model
    use_largest_model: bool  # godly: pick largest available model
    disable_critics: bool  # optimus: skip critic agents
    description: str
    prompt_color: str  # Rich color for prompt badge


MODE_CONFIGS: dict[SessionMode, ModeConfig] = {
    SessionMode.NORMAL: ModeConfig(
        mode=SessionMode.NORMAL,
        council_tier="auto",
        context_compression=False,
        max_context_tokens=16384,
        temperature=0.3,
        retrieval_depth=8,
        use_fastest_model=False,
        use_largest_model=False,
        disable_critics=False,
        description="Balanced — auto-selects council tier per task",
        prompt_color="cyan",
    ),
    SessionMode.OPTIMUS: ModeConfig(
        mode=SessionMode.OPTIMUS,
        council_tier="instant",
        context_compression=True,
        max_context_tokens=4096,
        temperature=0.1,
        retrieval_depth=3,
        use_fastest_model=True,
        use_largest_model=False,
        disable_critics=True,
        description="Speed mode — smallest model, compressed context, instant tier",
        prompt_color="yellow",
    ),
    SessionMode.GODLY: ModeConfig(
        mode=SessionMode.GODLY,
        council_tier="full",
        context_compression=False,
        max_context_tokens=128000,
        temperature=0.5,
        retrieval_depth=20,
        use_fastest_model=False,
        use_largest_model=True,
        disable_critics=False,
        description="Maximum — largest model, full context, complete council",
        prompt_color="magenta",
    ),
}


class ModeManager:
    def __init__(self) -> None:
        self._active = SessionMode.NORMAL

    @property
    def current(self) -> SessionMode:
        return self._active

    @property
    def config(self) -> ModeConfig:
        return MODE_CONFIGS[self._active]

    def set_mode(self, mode: SessionMode) -> ModeConfig:
        self._active = mode
        return self.config

    def is_normal(self) -> bool:
        return self._active == SessionMode.NORMAL
