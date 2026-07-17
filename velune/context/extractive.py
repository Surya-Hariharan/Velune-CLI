def compress_conversation(conversation: list[dict], max_tokens: int) -> list[dict]:
    """Drop oldest conversation turns until the total fits within max_tokens."""
    from velune.context.token_counter import estimate_tokens

    if not conversation:
        return conversation

    total = sum(estimate_tokens(m.get("content", "")) for m in conversation)
    if total <= max_tokens:
        return conversation

    result = list(conversation)
    while len(result) > 1:
        total = sum(estimate_tokens(m.get("content", "")) for m in result)
        if total <= max_tokens:
            break
        result = result[1:]

    return result
