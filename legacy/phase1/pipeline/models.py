"""Internal target schemas — the canonical shape our system expects.

These models are the destination, not the source. The config_loader
provides a key mapping that transforms whatever the upstream API sends
into this shape before validation.

If validation fails here, the error interceptor captures full telemetry
and hands it to the healing agent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr


class InternalAddress(BaseModel):
    model_config = ConfigDict(strict=True)

    street: StrictStr
    city: StrictStr
    state: StrictStr
    zip_code: StrictStr


class InternalUserProfile(BaseModel):
    """The single source of truth for a user profile inside our system."""

    model_config = ConfigDict(strict=True)

    user_id: StrictInt
    email_address: StrictStr
    full_name: StrictStr
    signup_date: StrictStr       # ISO 8601 string
    is_active: StrictBool
    address: InternalAddress
