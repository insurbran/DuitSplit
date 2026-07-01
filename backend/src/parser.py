"""AI layer: turn raw OCR text into a structured, validated Receipt."""

import json
import logging
import os
import time

from dotenv import load_dotenv
from google import genai
from pydantic import ValidationError

from models import Receipt, ReceiptItem

load_dotenv()

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-2.5-flash"
CONFIDENCE_THRESHOLD = 0.7

_PARSE_PROMPT = """You are a receipt parser. Given the raw OCR text of a receipt,
extract the structured data and return ONLY a JSON object (no markdown fences,
no commentary) with this exact shape:

{
  "items": [
    {"name": "string", "price": 0.00, "quantity": 1, "confidence": 0.0}
  ],
  "subtotal": 0.00,
  "tax_amount": 0.00,
  "tax_confidence": 0.0,
  "total": 0.00
}

Rules:
- "price" is the per-line total for that item (unit price * quantity if shown that
  way, otherwise the unit price). Use a number, not a string.
- "quantity" is an integer, default 1 if not shown.
- "confidence" is your confidence (0.0-1.0) that this line is a real purchasable
  item parsed correctly. Lower it when the text is ambiguous or garbled.
- "tax_amount" is the tax / service charge / GST / SST total as a single number.
  Keep it SEPARATE from item prices. Use 0 if the receipt shows no tax.
- "tax_confidence" is your confidence (0.0-1.0) in the tax amount.
- Exclude non-item lines (store name, address, payment method, change, etc.).
- Preserve EVERY line as its own entry. Do NOT merge, combine, or deduplicate
  repeated items — if the same item (e.g. "SOTONG KARI-(S)") appears on two
  separate lines, output it as two separate entries, not one.
- If subtotal/total are missing, infer subtotal as the sum of item prices and
  total as subtotal + tax_amount.
- Return valid JSON only.

RAW OCR TEXT:
---
{raw_text}
---
"""

_VALIDATE_PROMPT = """You are a strict classifier. Decide whether the following
text was extracted from a real purchase RECEIPT or bill (something with items and
prices that a group could split). Return ONLY a JSON object, no markdown fences:

{"is_receipt": true, "reason": "one short sentence"}

Set "is_receipt" to false for menus, random photos, screenshots, documents, or
anything that is not an itemised receipt/bill. Keep "reason" under 20 words.

TEXT:
---
{raw_text}
---
"""


def _get_client() -> genai.Client:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def parse_receipt(raw_text: str) -> Receipt:
    """Parse raw OCR text into a validated Receipt (with parse timing).

    Returns an empty Receipt on any failure (never raises to the caller).
    Items with confidence below CONFIDENCE_THRESHOLD are kept but logged as flagged.
    """
    start = time.perf_counter()

    if not raw_text or not raw_text.strip():
        logger.warning("parse_receipt received empty text; returning empty Receipt.")
        return Receipt()

    try:
        client = _get_client()
    except RuntimeError as exc:
        logger.error("Parser client init failed: %s", exc)
        return Receipt()

    prompt = _PARSE_PROMPT.replace("{raw_text}", raw_text)

    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        payload = _strip_code_fences(response.text or "")
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.error("Parser returned invalid JSON: %s", exc)
        return Receipt()
    except Exception as exc:  # noqa: BLE001 - graceful degradation required
        logger.error("Parser request failed: %s", exc)
        return Receipt()

    try:
        receipt = Receipt.model_validate(data)
    except ValidationError as exc:
        logger.error("Parsed data failed validation: %s", exc)
        receipt = _salvage(data)

    for item in receipt.items:
        if item.confidence < CONFIDENCE_THRESHOLD:
            logger.info(
                "Low-confidence item flagged: %s (%.2f)", item.name, item.confidence
            )

    # Derive the tax rate as a percentage of the subtotal, so the split can apply
    # it to each friend's own items.
    base = receipt.subtotal or sum(i.price for i in receipt.items)
    if base > 0:
        receipt.tax_percent = round(receipt.tax_amount / base * 100, 2)

    receipt.processing_ms = int((time.perf_counter() - start) * 1000)
    return receipt


def _salvage(data: object) -> Receipt:
    """Best-effort recovery when strict validation fails."""
    if not isinstance(data, dict):
        return Receipt()

    items: list[ReceiptItem] = []
    for raw in data.get("items", []) or []:
        if not isinstance(raw, dict):
            continue
        try:
            items.append(
                ReceiptItem(
                    name=str(raw.get("name", "Unknown")),
                    price=float(raw.get("price", 0.0)),
                    quantity=int(raw.get("quantity", 1) or 1),
                    confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.0)))),
                )
            )
        except (TypeError, ValueError):
            continue

    def _num(key: str) -> float:
        try:
            return float(data.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    subtotal = _num("subtotal") or sum(i.price for i in items)
    tax_amount = _num("tax_amount")
    tax_confidence = max(0.0, min(1.0, _num("tax_confidence")))
    total = _num("total") or (subtotal + tax_amount)
    return Receipt(
        items=items,
        subtotal=subtotal,
        tax_amount=tax_amount,
        tax_confidence=tax_confidence,
        total=total,
    )


def validate_receipt(raw_text: str) -> tuple[bool, str]:
    """Third AI pass: confirm the text really is a receipt.

    Returns (is_valid, reason). On any failure it fails open (True) so a working
    OCR result is never discarded because the validator call had a hiccup.
    """
    if not raw_text or not raw_text.strip():
        return False, "No text could be read from the image."

    try:
        client = _get_client()
    except RuntimeError as exc:
        logger.error("Validator client init failed: %s", exc)
        return True, ""

    prompt = _VALIDATE_PROMPT.replace("{raw_text}", raw_text)
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        data = json.loads(_strip_code_fences(response.text or ""))
        is_receipt = bool(data.get("is_receipt", True))
        reason = str(data.get("reason", "")).strip()
        return is_receipt, reason
    except json.JSONDecodeError as exc:
        logger.error("Validator returned invalid JSON: %s", exc)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - fail open on validator errors
        logger.error("Validator request failed: %s", exc)
        return True, ""
