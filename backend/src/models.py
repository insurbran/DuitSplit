"""Pydantic models for DuitSplit."""

import uuid

from pydantic import BaseModel, Field


class ReceiptItem(BaseModel):
    """A single line item parsed from a receipt.

    Each line gets a unique id so duplicate names (e.g. two "SAYUR" lines) stay
    independent when assigned to friends.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    price: float
    quantity: int = 1
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Receipt(BaseModel):
    """A fully parsed receipt. Tax is kept separate for proportional splitting."""

    items: list[ReceiptItem] = Field(default_factory=list)
    subtotal: float = 0.0
    tax_amount: float = 0.0
    # Tax as a percentage of the subtotal; each friend pays this % of their items.
    tax_percent: float = 0.0
    tax_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    total: float = 0.0
    # Third AI pass: whether the image actually looks like a receipt.
    is_valid_receipt: bool = True
    validation_reason: str = ""
    # True when OCR text came from the cache instead of Gemini.
    cached: bool = False
    # Total OCR + parse time reported to the user.
    processing_ms: int = 0


class Friend(BaseModel):
    """A person within a session. Friends are scoped to one session."""

    id: str
    session_id: str = ""
    name: str
    avatar_color: str
    total_owed: float = 0.0
    is_paid: bool = False


class ItemShare(BaseModel):
    """One item's cost attributed to a single friend."""

    id: str = ""
    name: str
    price: float


class FriendShare(BaseModel):
    """A friend's computed portion of the bill."""

    friend_id: str
    name: str
    avatar_color: str
    items: list[ItemShare] = Field(default_factory=list)
    subtotal: float = 0.0
    tax_share: float = 0.0
    total_owed: float = 0.0
    is_paid: bool = False


class Session(BaseModel):
    """A bill-splitting session (the Gold layer record)."""

    id: str
    name: str
    created_by: str
    receipt: Receipt
    tax_amount: float = 0.0
    qr_image_path: str = ""
    silver_path: str = ""
    friends: list[Friend] = Field(default_factory=list)
    # item_name -> list of friend ids sharing that item
    assignments: dict[str, list[str]] = Field(default_factory=dict)


class BillSummary(BaseModel):
    """The computed result of splitting a bill, with payment tracking."""

    session_id: str
    paid_by: str = ""
    qr_image_path: str = ""
    friends: list[FriendShare] = Field(default_factory=list)
    total: float = 0.0
    paid: float = 0.0
    remaining: float = 0.0


class SessionSummary(BaseModel):
    """A compact overview of one active session, for the home-page list."""

    id: str
    name: str
    created_by: str
    total_amount: float = 0.0
    paid_amount: float = 0.0
    remaining_amount: float = 0.0
    friends_total: int = 0
    friends_paid: int = 0
    created_at: str = ""
