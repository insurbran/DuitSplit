"""AI layer: turn raw OCR text into a structured, validated Receipt."""

from __future__ import annotations

import json
import logging
import os

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
  "tax": 0.00,
  "total": 0.00
}

Rules:
- "price" is the per-line total for that item (price * quantity if shown that way,
  otherwise the unit price). Use a number, not a string.
- "quantity" is an integer, default 1 if not shown.
- "confidence" is your confidence (0.0-1.0) that this line is a real purchasable
  item parsed correctly. Lower it when the text is ambiguous or garbled.
- Exclude non-item lines (store name, address, payment method, change, etc.).
- If subtotal/tax/total are missing, infer subtotal as the sum of item prices,
  tax as 0.0, and total as subtotal + tax.
- Return valid JSON only.

RAW OCR TEXT:
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
    """Parse raw OCR text into a validated Receipt.

    Returns an empty Receipt on any failure (never raises to the caller).
    Items with confidence below CONFIDENCE_THRESHOLD are kept but logged as flagged.
    """
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
    tax = _num("tax")
    total = _num("total") or (subtotal + tax)
    return Receipt(items=items, subtotal=subtotal, tax=tax, total=total)
