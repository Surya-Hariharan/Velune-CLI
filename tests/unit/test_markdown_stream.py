from __future__ import annotations

from rich.console import Console

from velune.cli.rendering import CustomMarkdown, MarkdownStreamBuffer


def test_markdown_stream_buffer_incremental_fences():
    buffer = MarkdownStreamBuffer()
    buffer.append("Here is some code:\n")
    buffer.append("```python\n")
    buffer.append("import os\n")

    # Render intermediate: check that it parses successfully
    r1 = buffer.get_renderable()
    assert isinstance(r1, CustomMarkdown)

    # Test that incomplete backticks at the end of the line are stripped to prevent flickering
    buffer.append("\n`")
    r2 = buffer.get_renderable()
    assert r2.markup.rstrip().endswith("import os")  # "\n`" is stripped

    buffer.append("`")
    r3 = buffer.get_renderable()
    assert r3.markup.rstrip().endswith("import os")  # "\n``" is stripped

    # Completing the fence closes the block
    buffer.append("`\n")
    r4 = buffer.get_renderable()
    assert r4.markup.rstrip().endswith("import os\n\n```")  # Fully completed fence is intact


def test_custom_markdown_code_block_line_numbers():
    console = Console(width=80, force_terminal=False, color_system=None)

    # 1. Short code block (<= 5 lines) should NOT show line numbers
    short_code = "```python\ndef hello():\n    print('world')\n```"
    md_short = CustomMarkdown(short_code)
    with console.capture() as capture:
        console.print(md_short)
    short_output = capture.get()

    # The lines are def hello(): and print('world').
    # Let's assert there's no line number prefix (like "1 " or "1 |")
    assert "1 def hello():" not in short_output
    assert "1 │ def hello():" not in short_output

    # 2. Long code block (> 5 lines) SHOULD show line numbers
    long_code = "```python\nline 1\nline 2\nline 3\nline 4\nline 5\nline 6\n```"
    md_long = CustomMarkdown(long_code)
    with console.capture() as capture:
        console.print(md_long)
    long_output = capture.get()

    # Check that line numbers are present
    assert "1" in long_output
    assert "6" in long_output
