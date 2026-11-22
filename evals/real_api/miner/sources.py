"""Spec repositories the miner draws cases from."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Source:
    api: str
    repo_url: str
    spec_path: str
    # (old_ref, new_ref) pairs; chosen from history later
    pairs: list[tuple[str, str]] = field(default_factory=list)


SOURCES: list[Source] = [
    Source(
        api="github",
        repo_url="https://github.com/github/rest-api-description",
        spec_path="descriptions/api.github.com/api.github.com.json",
    ),
    Source(
        api="stripe",
        repo_url="https://github.com/stripe/openapi",
        spec_path="openapi/spec3.json",
    ),
]
