"""Cognitive Firewall for shielding LLM prompts and repository indexing from prompt injection."""

from __future__ import annotations

import re
import logging
import unicodedata
from typing import Dict, Any, List, Optional

logger = logging.getLogger("velune.cognition.firewall")


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
        homoglyphs = {
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
        trans_table = str.maketrans(homoglyphs)
        return text.translate(trans_table)

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

    def scan_conversation(self, messages: List[Dict]) -> bool:
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
        
        # Individual message scanning
        for msg in messages:
            if msg.get("role") == "system":
                continue
            if not self.scan_text(msg.get("content", "")):
                return False
        
        return True

    def sanitize_content(self, text: str) -> str:
        """Neutralize malicious injection blocks in text by escaping structure tags and keywords."""
        sanitized = text
        # Neutralize common markdown system-directive keywords by adding slight spacing or escaping
        sanitized = re.sub(r"(?i)\bignore\b\s+\bprevious\b\s+\binstructions\b", "i_g_n_o_r_e previous instructions", sanitized)
        sanitized = re.sub(r"(?i)\bignore\b\s+\babove\b\s+\binstructions\b", "i_g_n_o_r_e above instructions", sanitized)
        sanitized = re.sub(r"(?i)\bignore\b\s+\bbelow\b\s+\binstructions\b", "i_g_n_o_r_e below instructions", sanitized)
        
        # Escape potential XML/HTML injection tags inside templates
        sanitized = sanitized.replace("<", "&lt;").replace(">", "&gt;")
        return sanitized

    def wrap_workspace_content(self, content_name: str, content: str) -> str:
        """Encapsulate code/text contents inside strict XML blocks.
        
        This prevents raw contents from escaping their boundaries and being interpreted 
        as active LLM instructions.
        """
        escaped_name = self.sanitize_content(content_name)
        escaped_content = self.sanitize_content(content)
        return (
            f"<workspace_file_content name=\"{escaped_name}\">\n"
            f"{escaped_content}\n"
            f"</workspace_file_content>"
        )

    def scan_file_for_injection(self, file_path: str, content: str) -> dict[str, Any]:
        """Perform a complete security scan on a workspace file before ingestion.
        
        Returns a dict indicating safety status, potential matched patterns, and a quarantined/neutralized content string.
        """
        is_safe = self.scan_text(content)
        neutralized = self.sanitize_content(content)
        
        return {
            "file_path": file_path,
            "is_safe": is_safe,
            "neutralized_content": neutralized,
            "quarantined": not is_safe,
        }
