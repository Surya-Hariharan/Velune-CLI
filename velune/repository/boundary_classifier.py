"""Boundary classifier for identifying critical code locations.

Classifies files/modules as sitting at critical system boundaries:
- Authentication (auth, login, JWT, OAuth, tokens, sessions)
- API surface (routes, endpoints, handlers, controllers)
- Database (models, schema, migrations, repositories)
- Payment (billing, Stripe, invoices, subscriptions)
- Event systems (events, pubsub, Kafka, webhooks)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("velune.repository.boundary_classifier")


class BoundaryType(StrEnum):
    """Critical system boundaries."""

    AUTHENTICATION = "authentication"
    API_SURFACE = "api_surface"
    DATABASE = "database"
    PAYMENT = "payment"
    EVENT_SYSTEM = "event_system"


@dataclass
class BoundaryPatterns:
    """Configurable patterns for boundary detection."""

    authentication: list[str] = None
    api_surface: list[str] = None
    database: list[str] = None
    payment: list[str] = None
    event_system: list[str] = None

    def __post_init__(self) -> None:
        """Initialize with default patterns if not provided."""
        if self.authentication is None:
            self.authentication = [
                "auth",
                "login",
                "jwt",
                "oauth",
                "token",
                "password",
                "credential",
                "session",
                "security",
                "permission",
            ]

        if self.api_surface is None:
            self.api_surface = [
                "routes",
                "endpoints",
                "api",
                "views",
                "controller",
                "handler",
                "middleware",
                "rest",
                "http",
                "request",
            ]

        if self.database is None:
            self.database = [
                "models",
                "schema",
                "migration",
                "repository",
                "dao",
                "orm",
                "database",
                "sql",
                "postgres",
                "mongodb",
            ]

        if self.payment is None:
            self.payment = [
                "payment",
                "billing",
                "stripe",
                "invoice",
                "checkout",
                "subscription",
                "transaction",
                "cart",
                "purchase",
                "paypal",
            ]

        if self.event_system is None:
            self.event_system = [
                "events",
                "pubsub",
                "kafka",
                "rabbitmq",
                "websocket",
                "webhook",
                "queue",
                "message",
                "broker",
                "emit",
            ]


class BoundaryClassifier:
    """Classifies code locations by system boundary criticality.

    Uses pattern matching on file paths and content to identify
    files that sit at critical boundaries (auth, API, DB, payment, events).
    """

    def __init__(self, patterns: BoundaryPatterns | None = None) -> None:
        """Initialize with optional custom patterns.

        Parameters
        ----------
        patterns:
            Custom boundary patterns; uses defaults if None.
        """
        self.patterns = patterns or BoundaryPatterns()

    def classify(self, file_path: str, content: str = "") -> BoundaryType | None:
        """Classify a file by its boundary type.

        Checks file path first (most efficient), then content if needed.
        Returns the most specific boundary type found, or None.

        Parameters
        ----------
        file_path:
            The file path (relative or absolute).
        content:
            Optional file content for deeper analysis.

        Returns
        -------
        BoundaryType | None:
            The boundary type if matched, None otherwise.
        """
        path_lower = file_path.lower()

        # Check authentication patterns
        if self._matches_pattern(path_lower, self.patterns.authentication):
            return BoundaryType.AUTHENTICATION

        # Check API surface patterns
        if self._matches_pattern(path_lower, self.patterns.api_surface):
            return BoundaryType.API_SURFACE

        # Check database patterns
        if self._matches_pattern(path_lower, self.patterns.database):
            return BoundaryType.DATABASE

        # Check payment patterns
        if self._matches_pattern(path_lower, self.patterns.payment):
            return BoundaryType.PAYMENT

        # Check event system patterns
        if self._matches_pattern(path_lower, self.patterns.event_system):
            return BoundaryType.EVENT_SYSTEM

        # If content provided, check file content as well
        if content:
            content_lower = content.lower()
            if self._matches_pattern(content_lower, self.patterns.authentication):
                return BoundaryType.AUTHENTICATION
            if self._matches_pattern(content_lower, self.patterns.api_surface):
                return BoundaryType.API_SURFACE
            if self._matches_pattern(content_lower, self.patterns.database):
                return BoundaryType.DATABASE
            if self._matches_pattern(content_lower, self.patterns.payment):
                return BoundaryType.PAYMENT
            if self._matches_pattern(content_lower, self.patterns.event_system):
                return BoundaryType.EVENT_SYSTEM

        return None

    def classify_by_path_only(self, file_path: str) -> BoundaryType | None:
        """Classify a file using only its path (fast path).

        Parameters
        ----------
        file_path:
            The file path to classify.

        Returns
        -------
        BoundaryType | None:
            The boundary type if matched, None otherwise.
        """
        return self.classify(file_path, content="")

    def batch_classify(self, file_paths: list[str]) -> dict[str, BoundaryType | None]:
        """Classify multiple files efficiently.

        Parameters
        ----------
        file_paths:
            List of file paths to classify.

        Returns
        -------
        dict[str, BoundaryType | None]:
            Mapping from file path to boundary type (or None).
        """
        return {path: self.classify_by_path_only(path) for path in file_paths}

    @staticmethod
    def _matches_pattern(text: str, patterns: list[str]) -> bool:
        """Check if any pattern matches the text.

        Uses word-boundary aware matching (separated by /, _, -, or whitespace).

        Parameters
        ----------
        text:
            The text to check (should be lowercase).
        patterns:
            List of patterns to match (should be lowercase).

        Returns
        -------
        bool:
            True if any pattern matches.
        """
        # Split text by common separators for better matching
        tokens = _tokenize(text)

        for pattern in patterns:
            if pattern in tokens:
                return True

        return False

    def get_all_boundaries_for(self, file_path: str) -> list[BoundaryType]:
        """Get all boundaries that match a file (not just the first).

        Parameters
        ----------
        file_path:
            The file path to analyze.

        Returns
        -------
        list[BoundaryType]:
            All matching boundary types, in priority order.
        """
        boundaries = []
        path_lower = file_path.lower()

        if self._matches_pattern(path_lower, self.patterns.authentication):
            boundaries.append(BoundaryType.AUTHENTICATION)
        if self._matches_pattern(path_lower, self.patterns.api_surface):
            boundaries.append(BoundaryType.API_SURFACE)
        if self._matches_pattern(path_lower, self.patterns.database):
            boundaries.append(BoundaryType.DATABASE)
        if self._matches_pattern(path_lower, self.patterns.payment):
            boundaries.append(BoundaryType.PAYMENT)
        if self._matches_pattern(path_lower, self.patterns.event_system):
            boundaries.append(BoundaryType.EVENT_SYSTEM)

        return boundaries


def _tokenize(text: str) -> set[str]:
    """Split text into tokens by common separators.

    Parameters
    ----------
    text:
        The text to tokenize (should be lowercase).

    Returns
    -------
    set[str]:
        Set of tokens.
    """
    import re

    # Replace common separators with spaces, then split
    text = re.sub(r"[/_\-.]", " ", text)
    return set(text.split())
