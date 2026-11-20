"""Data ingestion pipeline.

Flow:
  1. Fetch raw JSON from upstream API
  2. Load active field mapping from Postgres
  3. Remap raw keys → internal keys using the mapping
  4. Validate each record against InternalUserProfile (strict Pydantic v2)
  5. On success: return validated records (Phase 4 adds persistence)
  6. On failure: intercept error, build ErrorContext, log + return context

The ingestor never calls the healing agent directly.
The caller (or a future orchestrator) decides what to do with the ErrorContext.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from pipeline.config_loader import MappingConfig, load_active_mapping
from pipeline.error_interceptor import ErrorContext, intercept_validation_error
from pipeline.models import InternalAddress, InternalUserProfile

logger = logging.getLogger(__name__)

SOURCE_NAME = "vendor_profile_api"


@dataclass(slots=True)
class IngestionResult:
    success: bool
    records: list[InternalUserProfile]
    error_context: ErrorContext | None = None
    raw_payload: list[dict[str, Any]] | None = None


def fetch_upstream(api_url: str, timeout: float = 10.0) -> list[dict[str, Any]]:
    """GET the upstream API and return raw JSON records."""
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(api_url)
        resp.raise_for_status()
        data = resp.json()

    # Handle both list and single-object responses
    if isinstance(data, dict):
        data = [data]
    return data


def remap_record(
    raw: dict[str, Any],
    mapping: dict[str, str],
) -> dict[str, Any]:
    """Apply source→target key mapping to a single raw record.

    Keys not in the mapping are passed through unchanged.
    Nested objects are NOT remapped here — the agent (Phase 3) will handle
    nested key transformations via a richer mapping config.
    """
    remapped: dict[str, Any] = {}
    for source_key, value in raw.items():
        target_key = mapping.get(source_key, source_key)
        remapped[target_key] = value
    return remapped


def validate_records(
    remapped: list[dict[str, Any]],
) -> list[InternalUserProfile]:
    """Validate remapped records against the strict internal schema.

    Raises ValidationError on the FIRST record that fails.
    In production you'd want batch error collection — fine for Phase 1.
    """
    validated = []
    for record in remapped:
        # Handle nested address separately
        addr_data = record.get("address")
        if isinstance(addr_data, dict):
            record["address"] = InternalAddress(**addr_data)
        validated.append(InternalUserProfile(**record))
    return validated


def run_pipeline(api_url: str) -> IngestionResult:
    """Execute the full ingestion pipeline. Returns structured result."""

    # 1. Fetch
    logger.info("Fetching from %s", api_url)
    raw_payload = fetch_upstream(api_url)
    logger.info("Received %d record(s)", len(raw_payload))

    # 2. Load mapping
    mapping_config: MappingConfig = load_active_mapping(SOURCE_NAME)
    logger.info(
        "Loaded mapping v%d with %d field(s)",
        mapping_config.version,
        len(mapping_config.mappings),
    )

    # 3. Remap
    source_to_target = mapping_config.as_source_to_target()
    remapped = [remap_record(r, source_to_target) for r in raw_payload]

    # 4. Validate
    try:
        records = validate_records(remapped)
        logger.info("Validation passed — %d record(s) OK", len(records))
        return IngestionResult(success=True, records=records, raw_payload=raw_payload)

    except ValidationError as exc:
        target_fields = list(InternalUserProfile.model_fields.keys())
        error_ctx = intercept_validation_error(
            error=exc,
            raw_payload=raw_payload,
            mapping_config=mapping_config,
            target_model_fields=target_fields,
        )
        logger.error("Pipeline failed — ErrorContext ready for healing agent")
        return IngestionResult(
            success=False,
            records=[],
            error_context=error_ctx,
            raw_payload=raw_payload,
        )


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    base = os.environ.get("MOCK_API_BASE_URL", "http://localhost:8100")
    # Default to v1 (good). Pass --drift or set API_VERSION=v2 to break it.
    version = "v2" if "--drift" in sys.argv else os.environ.get("API_VERSION", "v1")
    url = f"{base}/{version}/profile"

    result = run_pipeline(url)

    if result.success:
        print(f"\n{'='*60}")
        print(f"  SUCCESS — {len(result.records)} record(s) validated")
        print(f"{'='*60}")
        for r in result.records:
            print(f"  {r.user_id} | {r.full_name} | {r.email_address}")
    else:
        print(f"\n{'='*60}")
        print(f"  PIPELINE FAILED — schema drift detected")
        print(f"{'='*60}")
        assert result.error_context is not None
        print(f"  Source:   {result.error_context.source_name}")
        print(f"  Version:  {result.error_context.mapping_version}")
        print(f"  Errors:   {len(result.error_context.validation_errors)}")
        print(f"\n  Raw payload keys:      {result.error_context.raw_payload_keys}")
        print(f"  Expected internal keys: {result.error_context.expected_keys}")
        print(f"\n  Error details:")
        for err in result.error_context.validation_errors:
            print(f"    - {err['field']}: {err['message']} (type={err['type']})")
        print(f"\n  Full prompt context for healing agent:")
        print(result.error_context.to_prompt_context())


if __name__ == "__main__":
    main()
