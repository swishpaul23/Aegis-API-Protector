"""Resolve a meaning-drift fix by keeping the data: the new values are the new normal."""

from __future__ import annotations

from ..store.files import FileStore
from .profile import load_profile, reset_fields, save_profile


def keep_data(store: FileStore, pending_id: int) -> list[str]:
    """Mark the fix kept and reset the baseline of its fields. Returns the reset paths."""
    root = getattr(store, "root", None)
    if root is None:
        raise ValueError("this store has no folder for profiles")
    item = store.get_pending(pending_id)
    paths = list(dict.fromkeys(f.path for f in item.findings))
    store.keep_pending(pending_id)
    profile = load_profile(root, item.model_key)
    reset_fields(profile, paths)
    save_profile(root, profile)
    return paths
