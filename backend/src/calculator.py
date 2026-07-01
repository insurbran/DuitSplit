"""Logic layer: split a bill among friends. Pure Python, no AI."""

from __future__ import annotations

from models import BillSummary, Session


def _round2(value: float) -> float:
    return round(value + 1e-9, 2)


def calculate_bill(session: Session) -> BillSummary:
    """Compute how much each friend owes for a session.

    - Each item's cost is split equally among the friends assigned to it.
    - Tax is distributed proportionally to each friend's share of the subtotal.
    - Unassigned items are ignored (no one is charged for them).
    """
    breakdown: dict[str, float] = {friend.id: 0.0 for friend in session.friends}

    receipt = session.receipt
    item_prices = {item.name: item.price for item in receipt.items}

    # Step 1: split each assigned item equally among its sharers.
    assigned_subtotal = 0.0
    for item_name, friend_ids in session.assignments.items():
        sharers = [fid for fid in friend_ids if fid in breakdown]
        if not sharers:
            continue
        price = item_prices.get(item_name)
        if price is None:
            continue
        share = price / len(sharers)
        for fid in sharers:
            breakdown[fid] += share
        assigned_subtotal += price

    # Step 2: distribute tax proportionally to each friend's pre-tax share.
    tax = receipt.tax or 0.0
    if tax and assigned_subtotal > 0:
        for fid, pre_tax in list(breakdown.items()):
            breakdown[fid] = pre_tax + tax * (pre_tax / assigned_subtotal)

    breakdown = {fid: _round2(amount) for fid, amount in breakdown.items()}
    total = _round2(sum(breakdown.values()))

    return BillSummary(
        session_id=session.id,
        breakdown=breakdown,
        total=total,
        paid_by=session.created_by,
    )
