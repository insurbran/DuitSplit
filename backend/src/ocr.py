"""Data layer: extract raw text from a receipt image using Gemini Vision."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-2.5-flash"
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 12

_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text from this receipt image exactly "
    "as it appears, preserving line breaks. Include item names, prices, "
    "quantities, subtotal, tax, and total. Do not summarize or interpret."
)

_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _get_client() -> genai.Client:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)


def _guess_mime_type(image_path: Path) -> str:
    return _MIME_TYPES.get(image_path.suffix.lower(), "image/jpeg")


def extract_text_from_image(image_path: Path) -> str:
    """Send an image to Gemini Vision and return the transcribed text.

    Retries up to MAX_ATTEMPTS times with RETRY_DELAY_SECONDS between attempts.
    Returns an empty string on persistent failure (never raises to the caller).
    """
    image_path = Path(image_path)
    if not image_path.is_file():
        logger.error("Image not found: %s", image_path)
        return ""

    try:
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        logger.error("Could not read image %s: %s", image_path, exc)
        return ""

    mime_type = _guess_mime_type(image_path)

    try:
        client = _get_client()
    except RuntimeError as exc:
        logger.error("OCR client init failed: %s", exc)
        return ""

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    _OCR_PROMPT,
                ],
            )
            text = (response.text or "").strip()
            if text:
                return text
            logger.warning("OCR attempt %d returned empty text.", attempt)
        except Exception as exc:  # noqa: BLE001 - graceful degradation required
            last_error = exc
            logger.warning("OCR attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)

        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error("OCR failed after %d attempts. Last error: %s", MAX_ATTEMPTS, last_error)
    return ""
