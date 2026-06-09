"""Load field mappings from PostgreSQL.

This module is READ-ONLY by design. The healing agent (Phase 3) writes
new mappings through a separate write-path with audit logging.

The mapping dict looks like:
    {"user_id": "user_id", "email_address": "email_address", ...}
where key = source API key, value = our internal target key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import psycopg


@dataclass(frozen=True, slots=True)
class FieldMapping:
    source_key: str
    target_key: str
    value_type: str


@dataclass(slots=True)
class MappingConfig:
    source_name: str
    version: int
    mappings: list[FieldMapping] = field(default_factory=list)

    def as_source_to_target(self) -> dict[str, str]:
        """Return {source_key: target_key} for simple remapping."""
        return {m.source_key: m.target_key for m in self.mappings}

    def as_target_to_source(self) -> dict[str, str]:
        """Return {target_key: source_key} for reverse lookups."""
        return {m.target_key: m.source_key for m in self.mappings}


def _get_dsn() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'pipeline')} "
        f"user={os.environ.get('POSTGRES_USER', 'pipeline_admin')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )


def load_active_mapping(source_name: str) -> MappingConfig:
    """Fetch the latest active mapping version for a given source."""
    query = """
        SELECT version, source_key, target_key, value_type
        FROM field_mappings
        WHERE source_name = %s AND is_active = TRUE
        ORDER BY version DESC, id ASC
    """
    with psycopg.connect(_get_dsn()) as conn, conn.cursor() as cur:
        cur.execute(query, (source_name,))
        rows = cur.fetchall()

    if not rows:
        raise RuntimeError(f"No active mappings found for source '{source_name}'")

    # All rows should share the highest version
    latest_version = rows[0][0]
    mappings = [
        FieldMapping(source_key=r[1], target_key=r[2], value_type=r[3])
        for r in rows
        if r[0] == latest_version
    ]

    return MappingConfig(
        source_name=source_name,
        version=latest_version,
        mappings=mappings,
    )
