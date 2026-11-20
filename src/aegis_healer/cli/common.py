"""Shared CLI helpers: model loading, safe HTTP fetch, rendering."""

from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Any

import httpx
import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from aegis_healer.store.files import FileStore
from aegis_healer.types import FieldMapping

console = Console()

MAX_BYTES = 10_000_000


def load_model(spec: str) -> type[BaseModel]:
    """Import 'package.module:ClassName' and check it is a Pydantic model."""
    if ":" not in spec:
        raise typer.BadParameter("use the form module:ClassName, e.g. app.models:User")
    module_name, _, attr = spec.partition(":")
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        obj = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise typer.BadParameter(f"cannot import {spec}: {exc}") from exc
    if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
        raise typer.BadParameter(f"{spec} is not a Pydantic model")
    return obj


def parse_headers(raw: list[str]) -> dict[str, str]:
    headers = {}
    for h in raw:
        name, sep, value = h.partition(":")
        if not sep or not name.strip():
            raise typer.BadParameter(f"header must look like 'Name: value', got {h!r}")
        headers[name.strip()] = value.strip()
    return headers


def fetch_json(
    url: str, headers: dict[str, str], timeout: float = 10.0, max_bytes: int = MAX_BYTES
) -> Any:
    """GET a JSON document. http(s) only, size-capped, limited redirects."""
    scheme = httpx.URL(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"only http and https URLs are allowed, got {scheme or 'none'!r}")
    with httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=5) as client:
        with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"response is larger than {max_bytes} bytes")
                chunks.append(chunk)
    return json.loads(b"".join(chunks))


def get_store(db: str | None) -> FileStore:
    return FileStore(db or os.environ.get("AEGIS_DIR", ".aegis"))


def describe(m: FieldMapping) -> str:
    if m.transform == "value_map":
        return f"value_map {json.dumps(m.value_map)}"
    if m.transform == "concat":
        return f"concat (sep {m.separator!r})"
    return "-" if m.transform == "none" else m.transform


def mapping_table(mappings: list[FieldMapping], title: str | None = None) -> Table:
    table = Table(title=title, title_justify="left", header_style="bold")
    for col in ("Field", "From", "Transform", "Origin", "Confidence", "Why"):
        table.add_column(col)
    for m in mappings:
        origin = "matcher" if m.origin == "deterministic" else "agent"
        conf = f"{m.confidence:.2f}"
        style = "green" if m.confidence >= 0.9 else "yellow"
        table.add_row(
            m.target,
            " + ".join(m.sources),
            describe(m),
            origin,
            f"[{style}]{conf}[/{style}]",
            m.reason,
        )
    return table
