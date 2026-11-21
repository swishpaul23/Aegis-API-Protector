"""Shared CLI helpers: model loading, safe HTTP fetch, safe rendering."""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
from typing import Any

import httpx
import typer
from pydantic import BaseModel
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from aegis_healer.store.files import FileStore
from aegis_healer.types import FieldMapping, MeaningFinding

console = Console()

MAX_BYTES = 10_000_000
MAX_REDIRECTS = 5
TOTAL_TIMEOUT = 30.0
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe(value: Any) -> str:
    """Untrusted text (API keys, LLM reasons) for the terminal: no markup, no escapes."""
    return escape(_CONTROL.sub("?", str(value)))


def redact_url(url: str) -> str:
    """Hide query strings (often API keys) and userinfo before printing a URL."""
    try:
        u = httpx.URL(url)
    except Exception:
        return "<invalid url>"
    shown = u.copy_with(query=None, fragment=None, username=None, password=None)
    return str(shown) + ("?…" if u.query else "")


def fetch_error_text(exc: BaseException) -> str:
    """A fetch failure without its message: httpx messages embed the full URL (query keys)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def error_text(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def load_model(spec: str) -> type[BaseModel]:
    """Import 'package.module:ClassName' and check it is a Pydantic model."""
    if ":" not in spec:
        raise typer.BadParameter("use the form module:ClassName, e.g. app.models:User")
    module_name, _, attr = spec.partition(":")
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        obj = getattr(importlib.import_module(module_name), attr)
    except Exception as exc:  # the user's module can fail in any way on import
        raise typer.BadParameter(f"cannot import {spec}: {type(exc).__name__}: {exc}") from exc
    if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
        raise typer.BadParameter(f"{spec} is not a Pydantic model")
    return obj


def parse_headers(raw: list[str]) -> dict[str, str]:
    headers = {}
    for h in raw:
        name, sep, value = h.partition(":")
        if not sep or not name.strip():
            raise typer.BadParameter("header must look like 'Name: value'")
        headers[name.strip()] = value.strip()
    return headers


def _origin(u: httpx.URL) -> tuple[str, str, int | None]:
    return (u.scheme, u.host, u.port)


def fetch_json(
    url: str,
    headers: dict[str, str],
    timeout: float = 10.0,
    max_bytes: int = MAX_BYTES,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """GET a JSON document safely.

    http(s) only; size-capped; overall deadline; at most 5 redirects, followed by hand
    so the user's headers are only ever sent to the original origin and https never
    downgrades to http.
    """
    current = httpx.URL(url)
    if current.scheme not in ("http", "https"):
        raise ValueError(f"only http and https URLs are allowed, got {current.scheme or 'none'!r}")
    origin = _origin(current)
    deadline = time.monotonic() + TOTAL_TIMEOUT
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
        for _ in range(MAX_REDIRECTS + 1):
            send = headers if _origin(current) == origin else {}
            with client.stream("GET", current, headers=send) as resp:
                if resp.is_redirect:
                    nxt = current.join(resp.headers.get("location", ""))
                    if nxt.scheme not in ("http", "https"):
                        raise ValueError("redirect to a non-http(s) URL refused")
                    if current.scheme == "https" and nxt.scheme == "http":
                        raise ValueError("redirect from https to http refused")
                    current = nxt
                    continue
                resp.raise_for_status()
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"response is larger than {max_bytes} bytes")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"download took longer than {TOTAL_TIMEOUT:.0f}s")
                    chunks.append(chunk)
                return json.loads(b"".join(chunks))
    raise ValueError(f"more than {MAX_REDIRECTS} redirects")


def get_store(db: str | None) -> FileStore:
    return FileStore(db or os.environ.get("AEGIS_DIR", ".aegis"))


def describe(m: FieldMapping) -> str:
    scale = f"x{m.scale:.12g}" if m.scale is not None else ""
    if m.transform == "value_map":
        base = f"value_map {json.dumps(m.value_map)}"
    elif m.transform == "concat":
        base = f"concat (sep {m.separator!r})"
    elif m.transform == "none":
        base = "" if scale else "-"
    else:
        base = m.transform
    return f"{base} {scale}".strip() if scale else base


def mapping_table(mappings: list[FieldMapping], title: str | None = None) -> Table:
    table = Table(title=safe(title) if title else None, title_justify="left", header_style="bold")
    for col in ("Field", "From", "Transform", "Origin", "Confidence", "Why"):
        table.add_column(col)
    for m in mappings:
        origin = "matcher" if m.origin == "deterministic" else "agent"
        style = "green" if m.confidence >= 0.9 else "yellow"
        table.add_row(
            safe(m.target),
            safe(" + ".join(m.sources)),
            safe(describe(m)),
            origin,
            f"[{style}]{m.confidence:.2f}[/{style}]",
            safe(m.reason),
        )
    return table


def findings_table(findings: list[MeaningFinding], title: str | None = None) -> Table:
    """What changed in the data's meaning: field, kind, detail (no record values)."""
    table = Table(title=safe(title) if title else None, title_justify="left", header_style="bold")
    for col in ("Field", "Kind", "Detail", "Suggested fix"):
        table.add_column(col)
    for f in findings:
        fix = f"x{f.suggested_scale:.12g}" if f.suggested_scale is not None else "-"
        table.add_row(safe(f.path), safe(f.kind), safe(f.detail), safe(fix))
    return table
