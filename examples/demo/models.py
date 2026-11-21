"""The contract your pipeline expects (from Phase 1's InternalUserProfile)."""

from pydantic import BaseModel, ConfigDict


class Address(BaseModel):
    model_config = ConfigDict(strict=True)
    street: str
    city: str
    state: str
    zip_code: str


class UserProfile(BaseModel):
    model_config = ConfigDict(strict=True)
    user_id: int | str
    email_address: str
    full_name: str
    signup_date: str
    is_active: bool
    address: Address
