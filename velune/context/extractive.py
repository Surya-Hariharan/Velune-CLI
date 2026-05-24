import re
from typing import List
from collections import Counter

def extractive_compress(text: str, target_tokens: int) -> str:
    """
    Sentence-importance based extractive compression.
    Preserves: first sentence, last sentence, high-keyword-density sentences.
    Drops: boilerplate, repetitive content, low-information sentences.
    """
    sentences = _split_sentences(text)
    if not sentences:
        return text
    
    target_chars = target_tokens * 4  # Rough token→char estimate
    if len(text) <= target_chars:
        return text  # Already fits
    
    # Score each sentence
    word_freq = Counter(re.findall(r'\w+', text.lower()))
    total_words = sum(word_freq.values())
    
    scored = []
    for i, sentence in enumerate(sentences):
        score = _score_sentence(sentence, word_freq, total_words)
        # Boost first and last sentences
        if i == 0:
            score += 0.5
        if i == len(sentences) - 1:
            score += 0.3
        # Boost sentences with code-like content
        if any(c in sentence for c in ('def ', 'class ', 'import ', '() ->', ':=')):
            score += 0.4
        scored.append((score, i, sentence))
    
    # Greedily include highest-scoring sentences until budget exhausted
    scored.sort(reverse=True)
    selected = []
    char_count = 0
    selected_indices = set()
    
    for score, idx, sentence in scored:
        if char_count + len(sentence) <= target_chars:
            selected.append((idx, sentence))
            selected_indices.add(idx)
            char_count += len(sentence)
    
    # Sort selected sentences back to original order
    selected.sort(key=lambda x: x[0])
    result = ' '.join(s for _, s in selected)
    
    if len(text) > len(result) + 50:
        result += f"\n[COMPRESSED: {len(text)} → {len(result)} chars]"
    
    return result

def _score_sentence(sentence: str, word_freq: Counter, total: int) -> float:
    words = re.findall(r'\w+', sentence.lower())
    if not words:
        return 0.0
    # TF-IDF inspired: sum of term frequencies of uncommon words
    score = sum(1.0 / (word_freq[w] + 1) for w in words if len(w) > 3)
    return score / len(words)

def _split_sentences(text: str) -> List[str]:
    # Split on sentence endings, preserve code blocks intact
    return re.split(r'(?<=[.!?])\s+', text)
