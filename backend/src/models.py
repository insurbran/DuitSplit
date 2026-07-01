"""Pydantic models for DuitSplit."""

from pydantic import BaseModel, Field


class ReceiptItem(BaseModel):
    """A single line item parsed from a receipt."""

    name: str
    price: float
    quantity: int = 1
    confidence: float = Field(ge=0.0, le=1.0)


class Receipt(BaseModel):
    """A fully parsed receipt."""

    items: list[ReceiptItem] = Field(default_factory=list)
    subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0


class Friend(BaseModel):
    """A person who can be assigned items in a session."""

    id: str
    name: str
    avatar_color: str


class Session(BaseModel):
    """A bill-splitting session: a receipt plus friends and their assignments."""

    id: str
    name: str
    created_by: str
    receipt: Receipt
    friends: list[Friend] = Field(default_factory=list)
    # item_name -> list of friend ids sharing that item
    assignments: dict[str, list[str]] = Field(default_factory=dict)


class BillSummary(BaseModel):
    """The computed result of splitting a bill."""

    session_id: str
    # friend_id -> amount owed
    breakdown: dict[str, float] = Field(default_factory=dict)
    total: float = 0.0
    paid_by: str = ""
