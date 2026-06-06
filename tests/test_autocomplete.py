from prompt_toolkit.document import Document
from velune.cli.autocomplete import SlashCompleter


def test_slash_triggers_completions():
    c = SlashCompleter()
    doc = Document("/he")
    completions = list(c.get_completions(doc, None))
    names = [comp.text for comp in completions]
    assert "help" in names


def test_no_slash_no_completions():
    c = SlashCompleter()
    doc = Document("hello")
    completions = list(c.get_completions(doc, None))
    assert completions == []


def test_partial_match_filters():
    c = SlashCompleter()
    doc = Document("/mo")
    completions = list(c.get_completions(doc, None))
    names = [comp.text for comp in completions]
    assert "model" in names
    assert "models" in names
    assert "mode" in names
    # "memory" starts with "me", not "mo" — should not appear
    assert "memory" not in names


def test_me_prefix_matches_memory():
    c = SlashCompleter()
    doc = Document("/me")
    completions = list(c.get_completions(doc, None))
    names = [comp.text for comp in completions]
    assert "memory" in names


def test_model_id_completion():
    c = SlashCompleter(model_ids=["llama3:8b", "llama3:70b", "phi3:mini"])
    doc = Document("/model llama")
    completions = list(c.get_completions(doc, None))
    names = [comp.text for comp in completions]
    assert "llama3:8b" in names
    assert "llama3:70b" in names
    assert "phi3:mini" not in names


def test_exact_command_still_shows():
    c = SlashCompleter()
    doc = Document("/help")
    completions = list(c.get_completions(doc, None))
    names = [comp.text for comp in completions]
    assert "help" in names


def test_extra_commands_included():
    c = SlashCompleter(extra_commands=[("custom", "A custom command")])
    doc = Document("/cu")
    completions = list(c.get_completions(doc, None))
    names = [comp.text for comp in completions]
    assert "custom" in names


def test_model_space_no_partial_returns_all():
    c = SlashCompleter(model_ids=["llama3:8b", "phi3:mini"])
    doc = Document("/model ")
    completions = list(c.get_completions(doc, None))
    names = [comp.text for comp in completions]
    assert "llama3:8b" in names
    assert "phi3:mini" in names
