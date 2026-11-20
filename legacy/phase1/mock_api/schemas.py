"""Response schemas for the mock vendor API.

v1 = the original contract our pipeline was built against.
v2 = simulates real-world drift: renamed keys, nested wrappers, type changes.
"""

from __future__ import annotations

from pydantic import BaseModel


# ── v1: stable contract ──────────────────────────────────────

class AddressV1(BaseModel):
    street: str
    city: str
    state: str
    zip_code: str


class UserProfileV1(BaseModel):
    user_id: int
    email_address: str
    full_name: str
    signup_date: str          # ISO 8601
    is_active: bool
    address: AddressV1


# ── v2: drifted contract ─────────────────────────────────────
# Changes:
#   user_id      → userId  (camelCase)
#   email_address → emailAddress
#   full_name     → removed, split into first_name + last_name
#   signup_date   → signupTimestamp (now epoch int, not ISO str)
#   is_active     → status (string "active" | "inactive")
#   address       → wrapped in contactInfo.mailingAddress

class AddressV2(BaseModel):
    streetLine1: str
    city: str
    region: str               # was "state"
    postalCode: str           # was "zip_code"


class ContactInfoV2(BaseModel):
    mailingAddress: AddressV2


class UserProfileV2(BaseModel):
    userId: str               # was int, now string UUID
    emailAddress: str
    first_name: str
    last_name: str
    signupTimestamp: int      # epoch seconds
    status: str               # "active" | "inactive"
    contactInfo: ContactInfoV2
