"""Cognitive Firewall for shielding LLM prompts and repository indexing from prompt injection."""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger("velune.cognition.firewall")


# Programmatically construct UNICODE_CONFUSABLES mapping
UNICODE_CONFUSABLES = {}
for i in range(26):
    UNICODE_CONFUSABLES[chr(0x1D400 + i)] = chr(ord('A') + i)  # Bold Cap
    UNICODE_CONFUSABLES[chr(0x1D41A + i)] = chr(ord('a') + i)  # Bold Lower
    UNICODE_CONFUSABLES[chr(0x1D434 + i)] = chr(ord('A') + i)  # Italic Cap
    UNICODE_CONFUSABLES[chr(0x1D44E + i)] = chr(ord('a') + i)  # Italic Lower
    UNICODE_CONFUSABLES[chr(0x1D468 + i)] = chr(ord('A') + i)  # Bold Italic Cap
    UNICODE_CONFUSABLES[chr(0x1D482 + i)] = chr(ord('a') + i)  # Bold Italic Lower
    UNICODE_CONFUSABLES[chr(0x1D4A2 + i)] = chr(ord('A') + i)  # Script Cap
    UNICODE_CONFUSABLES[chr(0x1D4B6 + i)] = chr(ord('a') + i)  # Script Lower
    UNICODE_CONFUSABLES[chr(0x1D4D0 + i)] = chr(ord('A') + i)  # Bold Script Cap
    UNICODE_CONFUSABLES[chr(0x1D4E4 + i)] = chr(ord('a') + i)  # Bold Script Lower
    UNICODE_CONFUSABLES[chr(0x1D4FA + i)] = chr(ord('A') + i)  # Fraktur Cap
    UNICODE_CONFUSABLES[chr(0x1D50E + i)] = chr(ord('a') + i)  # Fraktur Lower
    UNICODE_CONFUSABLES[chr(0x1D538 + i)] = chr(ord('A') + i)  # Double-struck Cap
    UNICODE_CONFUSABLES[chr(0x1D54E + i)] = chr(ord('a') + i)  # Double-struck Lower
    UNICODE_CONFUSABLES[chr(0x1D56C + i)] = chr(ord('A') + i)  # Bold Fraktur Cap
    UNICODE_CONFUSABLES[chr(0x1D580 + i)] = chr(ord('a') + i)  # Bold Fraktur Lower
    UNICODE_CONFUSABLES[chr(0x1D5A0 + i)] = chr(ord('A') + i)  # Sans-serif Cap
    UNICODE_CONFUSABLES[chr(0x1D5B4 + i)] = chr(ord('a') + i)  # Sans-serif Lower
    UNICODE_CONFUSABLES[chr(0x1D5D4 + i)] = chr(ord('A') + i)  # Sans-serif Bold Cap
    UNICODE_CONFUSABLES[chr(0x1D5E8 + i)] = chr(ord('a') + i)  # Sans-serif Bold Lower
    UNICODE_CONFUSABLES[chr(0x1D608 + i)] = chr(ord('A') + i)  # Sans-serif Italic Cap
    UNICODE_CONFUSABLES[chr(0x1D61C + i)] = chr(ord('a') + i)  # Sans-serif Italic Lower
    UNICODE_CONFUSABLES[chr(0x1D63C + i)] = chr(ord('A') + i)  # Sans-serif Bold Italic Cap
    UNICODE_CONFUSABLES[chr(0x1D650 + i)] = chr(ord('a') + i)  # Sans-serif Bold Italic Lower
    UNICODE_CONFUSABLES[chr(0x1D670 + i)] = chr(ord('A') + i)  # Monospace Cap
    UNICODE_CONFUSABLES[chr(0x1D684 + i)] = chr(ord('a') + i)  # Monospace Lower

    # Fullwidth forms
    UNICODE_CONFUSABLES[chr(0xFF21 + i)] = chr(ord('A') + i)
    UNICODE_CONFUSABLES[chr(0xFF41 + i)] = chr(ord('a') + i)

    # Enclosed alphanumerics
    UNICODE_CONFUSABLES[chr(0x24B6 + i)] = chr(ord('A') + i)
    UNICODE_CONFUSABLES[chr(0x24D0 + i)] = chr(ord('a') + i)


MULTILINE_INJECTION_PATTERNS = [
    r"(?is)from\s+now\s+on[.,\s].*?you\s+(must|will|should|are)",
    r"(?is)ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|context)",
    r"(?is)disregard\s+(all\s+)?(previous|prior)\s+(instructions|rules)",
    r"(?is)your\s+(new\s+)?(real\s+)?(purpose|task|goal|instructions)\s+is",
    r"(?is)act\s+as\s+(if\s+you\s+are|a|an)\s+.{0,50}(different|not|no longer)",
]


class CognitiveFirewall:
    """Shields agent instruction templates and workspace reading from prompt injections and spillover."""

    def __init__(self) -> None:
        # Common prompt injection patterns (imperative command overrides)
        self.injection_patterns = [
            r"(?i)\bignore\b.*\b(previous|prior|above|below|all|your)?\b.*\b(instructions|rules|constraints|context)\b",
            r"(?i)\b(disregard|forget|dismiss|bypass)\b.*\b(instructions|rules|constraints|context)\b",
            r"(?i)\b(act as|pretend|roleplay|imagine)\b.*\b(you are|you're)\b.*\b(different|not|no longer)\b",
            r"(?i)\bdo not\b.*\b(follow|adhere|comply)\b.*\b(instructions|rules|guidelines)\b",
            r"(?i)\byour (real|true|actual)\b.*\b(purpose|goal|task|function)\b",
            r"(?i)```\s*(system|instruction)\s*```",
            r"(?i)---+\s*(system|instruction|override)\s*---+",
            r"(?i)base64.*decode",
            r"(?i)eval\s*\(",
            r"(?i)\byou\b.*\bare\b.*\ban?\b.*\b(assistant|agent|bot|coder|evaluator)\b",
            r"(?i)\bnew\b.*\binstructions\b",
            r"(?i)\bsystem\b.*\bprompt\b",
            r"(?i)\boverride\b.*\binstructions\b",
            r"(?i)\bdo\b.*\bnot\b.*\bvalidate\b",
            r"(?i)\[\s*(system|instruction|user|assistant)\s*\]",
            r"(?i)<\s*(system|instruction|user|assistant)\s*>",
        ]

    def _normalize_homoglyphs(self, text: str) -> str:
        # Map common Cyrillic, Greek, and other homoglyphs to Latin equivalents
        existing_homoglyphs = {
            'а': 'a', 'А': 'A',
            'в': 'b', 'В': 'B',
            'е': 'e', 'Е': 'E',
            'ѕ': 's', 'Ѕ': 'S',
            'і': 'i', 'І': 'I',
            'ј': 'j', 'Ј': 'J',
            'о': 'o', 'О': 'O',
            'р': 'p', 'Р': 'P',
            'с': 'c', 'С': 'C',
            'у': 'y', 'У': 'Y',
            'х': 'x', 'Х': 'X',
            'α': 'a', 'β': 'b', 'ε': 'e', 'κ': 'k', 'ο': 'o', 'ρ': 'p', 'τ': 't', 'υ': 'u', 'χ': 'x',
            'Ɩ': 'l', 'ɩ': 'i',
        }
        return text.translate(str.maketrans({**existing_homoglyphs, **UNICODE_CONFUSABLES}))

    def scan_text(self, text: str) -> bool:
        """Scan a given string for potential prompt injection signatures.

        Returns True if the text is safe, False if a potential injection is detected.
        """
        # Normalize unicode to catch homoglyph attacks
        normalized = unicodedata.normalize('NFKC', text)
        # Transliterate homoglyphs
        homoglyph_normalized = self._normalize_homoglyphs(normalized)
        # Also check ASCII-folded version
        ascii_folded = normalized.encode('ascii', 'ignore').decode('ascii')

        for check_text in [text, normalized, homoglyph_normalized, ascii_folded]:
            for pattern in self.injection_patterns:
                if re.search(pattern, check_text):
                    logger.warning("Potential prompt injection attempt blocked by Cognitive Firewall: %s", pattern)
                    try:
                        from velune.telemetry.cognition import CognitivePerformanceAnalytics
                        analytics = CognitivePerformanceAnalytics()
                        analytics.record_injection_attempt("scan_text", pattern)
                    except Exception:
                        pass
                    return False
        return True

    def scan_conversation(self, messages: list[dict]) -> bool:
        """Scan full conversation history for injection patterns that span messages."""
        # Concatenate all user messages and scan combined text
        combined = " ".join(
            msg["content"] for msg in messages
            if msg.get("role") == "user"
        )

        # Multi-turn patterns: instruction appearing across turns
        multi_turn_patterns = [
            r"(?i)(from now on|starting now|going forward).*\n.*you (must|will|should|are)",
            r"(?i)(from now on|starting now|going forward).*you (must|will|should|are)",
        ]

        for pattern in multi_turn_patterns:
            if re.search(pattern, combined):
                logger.warning("Multi-turn split prompt injection attempt blocked: %s", pattern)
                try:
                    from velune.telemetry.cognition import CognitivePerformanceAnalytics
                    analytics = CognitivePerformanceAnalytics()
                    analytics.record_injection_attempt("scan_conversation", pattern)
                except Exception:
                    pass
                return False

        # Individual message scanning — skip only non-conversational roles (e.g. system, tool).
        # Assistant messages are included so reflected injections in provider responses are caught.
        for msg in messages:
            if msg.get("role") not in ("user", "assistant"):
                continue
            if not self.scan_text(msg.get("content", "")):
                return False

        return True


    def sanitize_content(self, text: str, is_code: bool = False) -> str:
        """Neutralize malicious injection blocks in text by escaping structure tags and keywords."""
        sanitized = text
        # Neutralize common markdown system-directive keywords by adding slight spacing or escaping
        sanitized = re.sub(r"(?i)\bignore\b\s+\bprevious\b\s+\binstructions\b", "i_g_n_o_r_e previous instructions", sanitized)
        sanitized = re.sub(r"(?i)\bignore\b\s+\babove\b\s+\binstructions\b", "i_g_n_o_r_e above instructions", sanitized)
        sanitized = re.sub(r"(?i)\bignore\b\s+\bbelow\b\s+\binstructions\b", "i_g_n_o_r_e below instructions", sanitized)

        if is_code:
            # For code: DO NOT HTML-escape < and >
            # These are valid Python syntax
            return sanitized

        # Escape potential XML/HTML injection tags inside templates
        # Preserve common code arrow operators
        sanitized = sanitized.replace("->", "__ARROW_PLACEHOLDER__")
        sanitized = sanitized.replace("=>", "__FATARROW_PLACEHOLDER__")
        sanitized = sanitized.replace("<", "&lt;").replace(">", "&gt;")
        sanitized = sanitized.replace("__ARROW_PLACEHOLDER__", "->")
        sanitized = sanitized.replace("__FATARROW_PLACEHOLDER__", "=>")
        return sanitized

    def wrap_workspace_content(self, content_name: str, content: str) -> str:
        """Encapsulate code/text contents inside strict XML blocks.

        This prevents raw contents from escaping their boundaries and being interpreted
        as active LLM instructions.
        """
        is_code = False
        if content_name.endswith((".py", ".ts", ".js", ".go", ".rs")):
            is_code = True

        escaped_name = self.sanitize_content(content_name, is_code=False)
        escaped_content = self.sanitize_content(content, is_code=is_code)
        return (
            f'<workspace_file_content name="{escaped_name}">\n'
            f'{escaped_content}\n'
            f'</workspace_file_content>'
        )

    def _extract_injectable_strings(self, code: str) -> list[str]:
        """Extract strings likely to contain injected instructions."""
        import ast
        extracted = []
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                # Docstrings (module, class, function)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    if isinstance(node.value.value, str):
                        extracted.append(node.value.value)
                # String assignments (README patterns)
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            extracted.append(node.value.value)
        except SyntaxError:
            # Non-Python: scan raw text with multiline patterns
            extracted.append(code)
        return extracted

    def scan_file_for_injection(self, file_path: str, content: str) -> dict[str, Any]:
        """Perform a complete security security scan on a workspace file before ingestion.

        Returns a dict indicating safety status, potential matched patterns, and a quarantined/neutralized content string.
        """
        # Existing single-line scan
        is_safe = self.scan_text(content)

        if is_safe:
            # Additional multi-line scan for embedded strings
            injectable_strings = self._extract_injectable_strings(content)
            for s in injectable_strings:
                for pattern in MULTILINE_INJECTION_PATTERNS:
                    if re.search(pattern, s):
                        logger.warning(
                            "SECURITY: Multi-line injection detected in %s: %s",
                            file_path, pattern[:50]
                        )
                        is_safe = False
                        break
                if not is_safe:
                    break

        neutralized = content if is_safe else self.sanitize_content(content)

        return {
            "file_path": file_path,
            "is_safe": is_safe,
            "neutralized_content": neutralized,
            "quarantined": not is_safe,
        }
