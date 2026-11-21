"""The contract: amount is in dollars."""

from pydantic import BaseModel


class Payment(BaseModel):
    id: int
    amount: float
    currency: str
    paid_at: str
