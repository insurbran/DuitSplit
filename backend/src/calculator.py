"""Logic layer: split a bill among friends with proportional tax. Pure Python."""

from models import BillSummary, Friend, FriendShare, ItemShare, Receipt, Session


def _round2(value: float) -> float:
    return round(value + 1e-9, 2)


def _expand_units(receipt: Receipt) -> dict[str, tuple[str, float]]:
    """Expand line items into individual units keyed by "{item_id}#{n}"."""
    units: dict[str, tuple[str, float]] = {}
    for item in receipt.items:
        qty = item.quantity if item.quantity and item.quantity > 0 else 1
        unit_price = item.price / qty
        for n in range(qty):
            units[f"{item.id}#{n}"] = (item.name, unit_price)
    return units


def compute_shares(
    receipt: Receipt,
    assignments: dict[str, list[str]],
    friends: list[Friend],
) -> list[FriendShare]:
    """Compute each friend's items, subtotal, tax share, and total.

    - Each line item is expanded into its individual units (a qty-2 RM30 line
      becomes two RM15 units), so units can be assigned to different friends.
    - A unit's price is split equally among the friends assigned to it.
    - Tax is applied as a percentage of each friend's own item subtotal:
        friend_tax = friend_subtotal * (tax_percent / 100)

    `assignments` is keyed by unit id ("{item_id}#{n}") so duplicate names and
    multi-quantity lines stay independent.
    """
    units = _expand_units(receipt)
    acc: dict[str, dict] = {
        f.id: {"items": [], "subtotal": 0.0} for f in friends
    }

    for unit_id, friend_ids in assignments.items():
        sharers = [fid for fid in friend_ids if fid in acc]
        if not sharers:
            continue
        unit = units.get(unit_id)
        if unit is None:
            continue
        name, unit_price = unit
        share = unit_price / len(sharers)
        for fid in sharers:
            acc[fid]["items"].append(
                ItemShare(id=unit_id, name=name, price=_round2(share))
            )
            acc[fid]["subtotal"] += share

    tax_percent = receipt.tax_percent or 0.0
    shares: list[FriendShare] = []
    for friend in friends:
        subtotal = acc[friend.id]["subtotal"]
        tax_share = subtotal * tax_percent / 100.0
        total_owed = subtotal + tax_share
        shares.append(
            FriendShare(
                friend_id=friend.id,
                name=friend.name,
                avatar_color=friend.avatar_color,
                items=acc[friend.id]["items"],
                subtotal=_round2(subtotal),
                tax_share=_round2(tax_share),
                total_owed=_round2(total_owed),
                is_paid=friend.is_paid,
            )
        )
    return shares


def calculate_bill(session: Session) -> BillSummary:
    """Build the full bill summary (per-friend shares + payment totals)."""
    shares = compute_shares(session.receipt, session.assignments, session.friends)
    total = _round2(sum(s.total_owed for s in shares))
    paid = _round2(sum(s.total_owed for s in shares if s.is_paid))
    remaining = _round2(total - paid)
    return BillSummary(
        session_id=session.id,
        paid_by=session.created_by,
        qr_image_path=session.qr_image_path,
        friends=shares,
        total=total,
        paid=paid,
        remaining=remaining,
    )
