from velune.cli.modes import MODE_CONFIGS, ModeManager, SessionMode


def test_default_mode_is_normal():
    m = ModeManager()
    assert m.current == SessionMode.NORMAL


def test_set_optimus_changes_config():
    m = ModeManager()
    config = m.set_mode(SessionMode.OPTIMUS)
    assert config.council_tier == "instant"
    assert config.use_fastest_model is True
    assert config.disable_critics is True
    assert config.context_compression is True


def test_set_godly_changes_config():
    m = ModeManager()
    config = m.set_mode(SessionMode.GODLY)
    assert config.council_tier == "full"
    assert config.use_largest_model is True
    assert config.disable_critics is False
    assert config.max_context_tokens >= 128000


def test_reset_to_normal():
    m = ModeManager()
    m.set_mode(SessionMode.GODLY)
    m.set_mode(SessionMode.NORMAL)
    assert m.is_normal() is True


def test_all_modes_have_configs():
    for mode in SessionMode:
        assert mode in MODE_CONFIGS
        config = MODE_CONFIGS[mode]
        assert config.council_tier in ("auto", "instant", "minimal", "standard", "full")
        assert 0.0 <= config.temperature <= 1.0
