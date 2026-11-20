"""Mock vendor profile API.

Two endpoints return the same conceptual data in different shapes:
  GET /v1/profile  → original stable schema
  GET /v2/profile  → post-drift schema (renamed keys, nesting changes, type changes)

The pipeline starts pointed at /v1. We flip it to /v2 to trigger the break.
"""

from __future__ import annotations

import time

from fastapi import FastAPI
from schemas import (
    AddressV1,
    AddressV2,
    ContactInfoV2,
    UserProfileV1,
    UserProfileV2,
)

app = FastAPI(title="Mock Vendor Profile API", version="2.0.0")

# ── Fake data ────────────────────────────────────────────────

_V1_PROFILES: list[UserProfileV1] = [
    UserProfileV1(
        user_id=1001,
        email_address="alice@example.com",
        full_name="Alice Johnson",
        signup_date="2023-06-15T10:30:00Z",
        is_active=True,
        address=AddressV1(
            street="742 Evergreen Terrace",
            city="Springfield",
            state="IL",
            zip_code="62704",
        ),
    ),
    UserProfileV1(
        user_id=1002,
        email_address="bob@example.com",
        full_name="Bob Smith",
        signup_date="2024-01-22T08:00:00Z",
        is_active=False,
        address=AddressV1(
            street="123 Maple Drive",
            city="Shelbyville",
            state="IL",
            zip_code="62565",
        ),
    ),
]

_V2_PROFILES: list[UserProfileV2] = [
    UserProfileV2(
        userId="a3f1-b8c2-1001",
        emailAddress="alice@example.com",
        first_name="Alice",
        last_name="Johnson",
        signupTimestamp=int(time.mktime(time.strptime("2023-06-15", "%Y-%m-%d"))),
        status="active",
        contactInfo=ContactInfoV2(
            mailingAddress=AddressV2(
                streetLine1="742 Evergreen Terrace",
                city="Springfield",
                region="IL",
                postalCode="62704",
            )
        ),
    ),
    UserProfileV2(
        userId="a3f1-b8c2-1002",
        emailAddress="bob@example.com",
        first_name="Bob",
        last_name="Smith",
        signupTimestamp=int(time.mktime(time.strptime("2024-01-22", "%Y-%m-%d"))),
        status="inactive",
        contactInfo=ContactInfoV2(
            mailingAddress=AddressV2(
                streetLine1="123 Maple Drive",
                city="Shelbyville",
                region="IL",
                postalCode="62565",
            )
        ),
    ),
]


# ── Endpoints ────────────────────────────────────────────────

@app.get("/v1/profile", response_model=list[UserProfileV1])
async def get_profiles_v1() -> list[UserProfileV1]:
    """Original stable contract."""
    return _V1_PROFILES


@app.get("/v2/profile", response_model=list[UserProfileV2])
async def get_profiles_v2() -> list[UserProfileV2]:
    """Post-drift contract — breaks any consumer expecting v1 shape."""
    return _V2_PROFILES


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
