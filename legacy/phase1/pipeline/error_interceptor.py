"""Middleware error interceptor.

Wraps the Pydantic validation step. On failure, captures everything the
healing agent will need:
  - raw JSON payload that caused the failure
  - the Pydantic ValidationError details
  - the current mapping config from Postgres
  - a structured ErrorContext object ready for the agent's prompt

This does NOT call the agent — it only packages. The caller decides
whether to invoke the healing loop or just log and dead-letter.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from pipeline.config_loader import MappingConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ErrorContext:
    """Everything the healing agent needs to diagnose and fix a drift."""

    source_name: str
    timestamp: str
    raw_payload: list[dict[str, Any]]
    validation_errors: list[dict[str, Any]]
    current_mapping: dict[str, str]       # {source_key: target_key}
    mapping_version: int
    raw_payload_keys: list[str]           # top-level keys from first record
    expected_keys: list[str]              # what the target schema wants
    extra_context: dict[str, Any] = field(default_factory=dict)

    def to_prompt_context(self) -> str:
        """Serialize to a string suitable for an LLM prompt."""
        return json.dumps(
            {
                "source_name": self.source_name,
                "timestamp": self.timestamp,
                "mapping_version": self.mapping_version,
                "current_mapping": self.current_mapping,
                "expected_internal_keys": self.expected_keys,
                "actual_payload_keys": self.raw_payload_keys,
                "sample_raw_record": self.raw_payload[0] if self.raw_payload else {},
                "validation_errors": self.validation_errors,
            },
            indent=2,
        )


def intercept_validation_error(
    *,
    error: ValidationError,
    raw_payload: list[dict[str, Any]],
    mapping_config: MappingConfig,
    target_model_fields: list[str],
) -> ErrorContext:
    """Build an ErrorContext from a caught ValidationError.

    Args:
        error: The Pydantic ValidationError that was raised.
        raw_payload: The unparsed JSON records from the upstream API.
        mapping_config: The mapping config that was active when the error occurred.
        target_model_fields: Field names from the internal Pydantic model.

    Returns:
        A fully populated ErrorContext ready for the healing agent.
    """
    # Extract structured error details from Pydantic
    validation_errors = []
    for err in error.errors():
        validation_errors.append({
            "field": ".".join(str(loc) for loc in err["loc"]),
            "type": err["type"],
            "message": err["msg"],
            "input": _safe_repr(err.get("input")),
        })

    # Top-level keys from the first raw record
    raw_keys = sorted(raw_payload[0].keys()) if raw_payload else []

    ctx = ErrorContext(
        source_name=mapping_config.source_name,
        timestamp=datetime.now(timezone.utc).isoformat(),
        raw_payload=raw_payload,
        validation_errors=validation_errors,
        current_mapping=mapping_config.as_source_to_target(),
        mapping_version=mapping_config.version,
        raw_payload_keys=raw_keys,
        expected_keys=target_model_fields,
    )

    logger.error(
        "Validation failed for source=%s version=%d | %d error(s) | raw_keys=%s",
        mapping_config.source_name,
        mapping_config.version,
        len(validation_errors),
        raw_keys,
    )

    return ctx


def _safe_repr(value: Any, max_len: int = 200) -> str:
    """Truncate large values so they don't blow up the prompt."""
    s = repr(value)
    return s[:max_len] + "..." if len(s) > max_len else s
