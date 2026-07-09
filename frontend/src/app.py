"""DuitSplit frontend: serves the UI and proxies /api/* to the backend."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8001").rstrip("/")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="DuitSplit Frontend")

_HOP_BY_HOP = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
}


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    """Home page: list of active (unpaid) sessions to resume, plus New Session."""
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/new", response_class=HTMLResponse)
def new_session(request: Request) -> HTMLResponse:
    """Start a fresh split (the upload → review → assign → summary flow)."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "backend_url": BACKEND_URL, "session_id": ""},
    )


@app.get("/session/{session_id}", response_class=HTMLResponse)
def session_page(request: Request, session_id: str) -> HTMLResponse:
    """Resume an existing session: opens directly on the bill summary."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "backend_url": BACKEND_URL, "session_id": session_id},
    )


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy(path: str, request: Request) -> Response:
    """Forward /api/* requests to the backend, preserving method and body."""
    url = f"{BACKEND_URL}/{path}"
    body = await request.body()

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }

    # Preserve the real client IP so the backend can rate-limit per user,
    # not per (shared) proxy address.
    client_host = request.client.host if request.client else ""
    if client_host:
        existing = request.headers.get("x-forwarded-for")
        headers["X-Forwarded-For"] = (
            f"{existing}, {client_host}" if existing else client_host
        )

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            upstream = await client.request(
                request.method,
                url,
                content=body,
                params=dict(request.query_params),
                headers=headers,
            )
    except httpx.RequestError as exc:
        logger.error("Proxy error to %s: %s", url, exc)
        return Response(
            content='{"detail":"Backend unavailable."}',
            status_code=502,
            media_type="application/json",
        )

    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
