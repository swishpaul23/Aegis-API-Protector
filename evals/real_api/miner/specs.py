"""Fetch OpenAPI specs from git history and flatten schemas to typed dotted paths."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from evals.real_api.cases import FieldType

MAX_DEPTH = 4  # max inline nesting depth (dots in a path)
MAX_REF_HOPS = 1  # a referenced schema's own $refs are not followed
MAX_PATHS = 2000  # per-schema budget; expansion stops beyond it


def clone_repo(repo_url: str, repo_dir: Path) -> None:
    """Blobless, checkout-less clone; blobs are fetched lazily by `git show`."""
    subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", "--", repo_url, str(repo_dir)],
        check=True,
    )


def fetch_spec(repo_dir: Path, ref: str, spec_path: str) -> dict:
    if ref.startswith("-"):
        raise ValueError(f"invalid ref {ref!r}")
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"{ref}:{spec_path}"],
        check=True,
        capture_output=True,
    )
    return json.loads(out.stdout)


def _field_type(node: dict[str, Any]) -> FieldType | None:
    t = node.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        t = non_null[0] if len(non_null) == 1 else None
    if t == "string":
        return "datetime" if node.get("format") == "date-time" else "string"
    if t in ("integer", "number", "boolean"):
        return t
    return None


def flatten_schemas(spec: dict) -> dict[str, dict[str, FieldType]]:
    """Schema name -> dotted field path -> type. Objects become prefixes."""
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})

    def walk(
        node: Any, prefix: str, visiting: frozenset[str], depth: int, out: dict[str, FieldType]
    ) -> None:
        if not isinstance(node, dict) or len(out) >= MAX_PATHS:
            return
        if prefix.count(".") >= MAX_DEPTH:
            return
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name in visiting or depth >= MAX_REF_HOPS or name not in schemas:
                return
            walk(schemas[name], prefix, visiting | {name}, depth + 1, out)
            return
        if "oneOf" in node or "anyOf" in node:
            return
        for part in node.get("allOf", []):
            walk(part, prefix, visiting, depth, out)
        if "properties" in node:
            for key, child in node["properties"].items():
                if "." in key:
                    continue
                walk(child, f"{prefix}.{key}" if prefix else key, visiting, depth, out)
            return
        if node.get("type") == "array":
            return
        ftype = _field_type(node)
        if ftype and prefix:
            out[prefix] = ftype

    result: dict[str, dict[str, FieldType]] = {}
    for name, schema in schemas.items():
        fields: dict[str, FieldType] = {}
        walk(schema, "", frozenset({name}), 0, fields)
        result[name] = fields
    return result
