"""Data layer: OCR a receipt image with Gemini Vision and persist ETL artifacts.

ETL layers:
    Bronze -> raw image saved to data/bronze/{session_id}_receipt.jpg
    Silver -> extracted text saved to data/silver/{session_id}.txt
(The Gold layer — structured session data in SQLite — is handled by app.py.)
"""

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

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
BRONZE_DIR = DATA_DIR / "bronze"
SILVER_DIR = DATA_DIR / "silver"

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


MIN_IMAGE_BYTES = 2048


def _detect_image_format(data: bytes) -> str | None:
    """Identify a supported image by its magic bytes (no decoding, no AI)."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[4:8] == b"ftyp" and data[8:12] in (
        b"heic", b"heix", b"mif1", b"msf1", b"hevc",
    ):
        return "heic"
    return None


def precheck_image(data: bytes) -> tuple[bool, str]:
    """Cheap local validity check run BEFORE any Gemini call.

    Rejects empty files, non-image files, and tiny/corrupt uploads so obviously
    bad input never costs an API call. Returns (is_ok, reason).
    """
    if not data:
        return False, "The file is empty."
    if _detect_image_format(data) is None:
        return False, "That file is not a supported image (JPEG, PNG, WebP, HEIC)."
    if len(data) < MIN_IMAGE_BYTES:
        return False, "The image is too small or looks corrupted."
    return True, ""


def _get_client() -> genai.Client:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)


def _guess_mime_type(image_path: Path) -> str:
    return _MIME_TYPES.get(image_path.suffix.lower(), "image/jpeg")


def bronze_path(session_id: str) -> Path:
    return BRONZE_DIR / f"{session_id}_receipt.jpg"


def silver_path(session_id: str) -> Path:
    return SILVER_DIR / f"{session_id}.txt"


def save_bronze(session_id: str, image_bytes: bytes) -> Path:
    """Persist the raw receipt image (Bronze layer). Never raises."""
    path = bronze_path(session_id)
    try:
        BRONZE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image_bytes)
    except OSError as exc:
        logger.error("Failed to save bronze image %s: %s", path, exc)
    return path


def save_silver(session_id: str, text: str) -> Path:
    """Persist the extracted text (Silver layer). Never raises."""
    path = silver_path(session_id)
    try:
        SILVER_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to save silver text %s: %s", path, exc)
    return path


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


def run_ocr_pipeline(session_id: str, image_bytes: bytes) -> tuple[str, int]:
    """Bronze -> OCR -> Silver, measuring elapsed OCR time.

    Returns (raw_text, elapsed_ms). Bytes come from the upload so nothing else
    needs to touch the filesystem before OCR.
    """
    src = save_bronze(session_id, image_bytes)
    start = time.perf_counter()
    text = extract_text_from_image(src)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if text:
        save_silver(session_id, text)
    return text, elapsed_ms
