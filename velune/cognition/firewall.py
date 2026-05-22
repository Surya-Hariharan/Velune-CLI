"""Cognitive Firewall for shielding LLM prompts and repository indexing from prompt injection."""

from __future__ import annotations

import re
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("velune.cognition.firewall")


class CognitiveFirewall:
    """Shields agent instruction templates and workspace reading from prompt injections and spillover."""

    def __init__(self) -> None:
        # Common prompt injection patterns (imperative command overrides)
        self.injection_patterns = [
            r"(?i)\bignore\b.*\bprevious\b.*\binstructions\b",
            r"(?i)\bignore\b.*\babove\b.*\binstructions\b",
            r"(?i)\bignore\b.*\bbelow\b.*\binstructions\b",
            r"(?i)\byou\b.*\bare\b.*\ban?\b.*\b(assistant|agent|bot|coder|evaluator)\b",
            r"(?i)\bnew\b.*\binstructions\b",
            r"(?i)\bsystem\b.*\bprompt\b",
            r"(?i)\boverride\b.*\binstructions\b",
            r"(?i)\bdo\b.*\bnot\b.*\bvalidate\b",
            r"(?i)\[\s*(system|instruction|user|assistant)\s*\]",
            r"(?i)<\s*(system|instruction|user|assistant)\s*>",
        ]

    def scan_text(self, text: str) -> bool:
        """Scan a given string for potential prompt injection signatures.
        
        Returns True if the text is safe, False if a potential injection is detected.
        """
        for pattern in self.injection_patterns:
            if re.search(pattern, text):
                logger.warning("Potential prompt injection attempt blocked by Cognitive Firewall: %s", pattern)
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
