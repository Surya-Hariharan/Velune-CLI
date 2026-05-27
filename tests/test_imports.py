def test_all_modules_importable():
    """Verify all modules load without NameError or ImportError."""
    import velune.intent.parser
    import velune.intent.hypothesis
    import velune.context.stitcher
    import velune.kernel.health
    import velune.context.compressor
    # Instantiate classes that had type annotation failures
    from velune.intent.parser import IntentSignalParser
    parser = IntentSignalParser()
    result = parser.parse("fix the auth bug")
    assert isinstance(result, dict)
    assert "action_verbs" in result
