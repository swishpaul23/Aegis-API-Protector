from datetime import datetime

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr


class Address(BaseModel):
    city: str
    postal_code: str


class User(BaseModel):
    user_id: int
    user_name: str
    email: str
    signup_date: datetime
    address: Address


# Phase 1 internal models (strict), copied from legacy/phase1/pipeline/models.py
class InternalAddress(BaseModel):
    model_config = ConfigDict(strict=True)
    street: StrictStr
    city: StrictStr
    state: StrictStr
    zip_code: StrictStr


class InternalUserProfile(BaseModel):
    model_config = ConfigDict(strict=True)
    user_id: StrictInt
    email_address: StrictStr
    full_name: StrictStr
    signup_date: StrictStr
    is_active: StrictBool
    address: InternalAddress
