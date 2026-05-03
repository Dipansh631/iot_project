"""pdf_extractor.py — PDF text extraction using local parsing + Gemini.

This project previously used PDF.co for OCR/text extraction, but that API is
unreliable in some environments. This module now uses:

- Local text extraction via `pypdf` (fast, no API key needed)
- Optional Gemini fallback for scanned/image PDFs when `ocr_enabled=True`

Environment:
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) is required for Gemini fallback.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ── Gemini setup ─────────────────────────────────────────────────────────────

_GEMINI_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_GEMINI_MODEL_PDF = os.getenv("GEMINI_MODEL_PDF", "gemini-2.0-flash")
_GEMINI_MODEL_TEXT = os.getenv("GEMINI_MODEL_TEXT", "gemini-2.0-flash")

_gemini_pdf_model = None
_gemini_text_model = None

if _GEMINI_KEY:
    try:
        import google.generativeai as genai

        genai.configure(api_key=_GEMINI_KEY)
        _gemini_pdf_model = genai.GenerativeModel(_GEMINI_MODEL_PDF)
        _gemini_text_model = genai.GenerativeModel(_GEMINI_MODEL_TEXT)
    except Exception as exc:
        logger.warning(f"Gemini client init failed: {exc}")
        _gemini_pdf_model = None
        _gemini_text_model = None


def _require_gemini() -> None:
    if not _gemini_pdf_model:
        raise RuntimeError(
            "Gemini is not configured. Set GEMINI_API_KEY (or GOOGLE_API_KEY)."
        )


# ── Page-range parsing ───────────────────────────────────────────────────────

_PAGES_TOKEN_RE = re.compile(r"^\s*(\d+)(?:\s*-\s*(\d+))?\s*$")


def _parse_pages(pages: str, total_pages: int) -> list[int]:
    """Return 0-based page indices to process."""
    if total_pages <= 0:
        return []

    if not pages or not pages.strip():
        return list(range(total_pages))

    indices: set[int] = set()
    for token in pages.split(","):
        token = token.strip()
        if not token:
            continue
        m = _PAGES_TOKEN_RE.match(token)
        if not m:
            raise ValueError(f"Invalid pages format: '{pages}'")

        start = int(m.group(1))
        end = int(m.group(2) or m.group(1))
        if start < 1 or end < 1:
            raise ValueError("Pages are 1-based; use values >= 1")
        if end < start:
            start, end = end, start

        for p in range(start, end + 1):
            idx = p - 1
            if 0 <= idx < total_pages:
                indices.add(idx)

    return sorted(indices)


# ── Extraction helpers ───────────────────────────────────────────────────────


def _extract_text_local(file_bytes: bytes, pages: str = "") -> tuple[str, int]:
    """Best-effort local text extraction using `pypdf`."""
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError(
            "Missing dependency 'pypdf'. Install it with: pip install pypdf"
        ) from exc

    reader = PdfReader(io.BytesIO(file_bytes))
    page_indices = _parse_pages(pages, len(reader.pages))
    out: list[str] = []
    for idx in page_indices:
        try:
            txt = reader.pages[idx].extract_text() or ""
        except Exception:
            txt = ""
        if txt:
            out.append(txt)
    text = "\n\n".join(out).strip()
    return text, len(page_indices)


def _strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _extract_text_gemini_pdf(file_bytes: bytes, *, pages: str = "", lang: str = "eng") -> str:
    _require_gemini()
    page_hint = (
        "Extract ALL pages"
        if not pages
        else f"Extract ONLY these pages (1-based): {pages}"
    )
    prompt = (
        "Extract all readable text from this PDF. "
        f"{page_hint}. "
        "Preserve the original reading order as much as possible. "
        "Return plain text only (no markdown, no code fences). "
        "If the document is scanned, perform OCR. "
        f"Language hint: {lang}."
    )
    resp = _gemini_pdf_model.generate_content(
        [
            prompt,
            {"mime_type": "application/pdf", "data": file_bytes},
        ]
    )
    return _strip_code_fences(getattr(resp, "text", "") or "")


def _parse_json_from_model(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from model output (best effort)."""
    if not text:
        return None
    t = text.strip()
    # Find first {...} block
    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if not m:
        return None
    candidate = m.group(0)
    try:
        return json.loads(candidate)
    except Exception:
        return None

def extract_text_from_url(
    pdf_url: str,
    pages: str = "",
    lang: str = "eng",
    ocr_enabled: bool = True,
    inline_result: bool = True,
) -> dict:
    """
    Extract text from a PDF at a public URL.

    Parameters
    ----------
    pdf_url       : Publicly accessible URL of the PDF.
    pages         : Page range, e.g. "1-3,5" or "" for all pages.
    lang          : OCR language code (default "eng").
    ocr_enabled   : Enable OCR for scanned / image-based PDFs.
    inline_result : If True, return text directly instead of a download URL.

    Returns
    -------
    dict with keys:
        text       - extracted plain text
        page_count - number of pages processed
        source_url - original PDF URL
        body_url   - always "" (kept for backward compatibility)
    """
    try:
        resp = requests.get(pdf_url, timeout=60)
        resp.raise_for_status()
        file_bytes = resp.content
    except Exception as exc:
        raise RuntimeError(f"Failed to download PDF from URL: {exc}") from exc

    result = extract_text_from_bytes(
        file_bytes=file_bytes,
        filename="document.pdf",
        pages=pages,
        lang=lang,
        ocr_enabled=ocr_enabled,
    )
    result["source_url"] = pdf_url
    return result


def extract_text_from_bytes(
    file_bytes: bytes,
    filename: str = "document.pdf",
    pages: str = "",
    lang: str = "eng",
    ocr_enabled: bool = True,
) -> dict:
    """
    Extract text from PDF bytes.
    Uses local parsing first; if that yields little/no text and `ocr_enabled=True`,
    it falls back to Gemini OCR/document understanding.

    Parameters
    ----------
    file_bytes  : Raw bytes of the PDF file.
    filename    : Original filename (used for upload).
    pages       : Page range, e.g. "1-3,5" or "" for all pages.
    lang        : OCR language code.
    ocr_enabled : Enable OCR for scanned PDFs.

    Returns
    -------
    Same dict as extract_text_from_url().
    """
    if not file_bytes:
        raise ValueError("file_bytes is empty")

    local_text, page_count = _extract_text_local(file_bytes, pages=pages)

    # Heuristic: if we got meaningful text, return it.
    if local_text and len(local_text) >= 40:
        return {
            "text": local_text,
            "page_count": page_count,
            "source_url": "",
            "body_url": "",
        }

    if not ocr_enabled:
        return {
            "text": local_text or "",
            "page_count": page_count,
            "source_url": "",
            "body_url": "",
        }

    # Gemini fallback for scanned PDFs / images-in-PDF.
    gemini_text = _extract_text_gemini_pdf(file_bytes, pages=pages, lang=lang)
    return {
        "text": gemini_text.strip(),
        "page_count": page_count,
        "source_url": "",
        "body_url": "",
    }


def extract_text_from_file(
    file_path: str | Path,
    pages: str = "",
    lang: str = "eng",
    ocr_enabled: bool = True,
) -> dict:
    """
    Read a local PDF file and extract its text.

    Parameters
    ----------
    file_path   : Absolute or relative path to the PDF.
    pages       : Page range, e.g. "1-3,5" or "" for all pages.
    lang        : OCR language code.
    ocr_enabled : Enable OCR for scanned PDFs.

    Returns
    -------
    Same dict as extract_text_from_url().
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {file_path}")

    logger.info(f"Reading local PDF: {path.name}")
    file_bytes = path.read_bytes()
    return extract_text_from_bytes(
        file_bytes=file_bytes,
        filename=path.name,
        pages=pages,
        lang=lang,
        ocr_enabled=ocr_enabled,
    )


# ── Structured extraction (tables, forms) ──────────────────────────────────────

def extract_structured_data(
    pdf_url: str,
    output_format: str = "json",   # "json" or "csv"
) -> dict:
    """
    Extract tables / structured data from a PDF using Gemini.
    output_format: "json" for structured JSON, "csv" for tabular text.
    """
    # Download once, then pass as PDF bytes to Gemini.
    resp = requests.get(pdf_url, timeout=60)
    resp.raise_for_status()
    pdf_bytes = resp.content

    _require_gemini()
    if output_format not in ("json", "csv"):
        raise ValueError("output_format must be 'json' or 'csv'")

    if output_format == "json":
        prompt = (
            "Extract structured data from this PDF. "
            "Return ONLY valid JSON (no markdown). "
            "If the PDF is a form, output a JSON object with fields and values. "
            "If the PDF contains tables, output an array of rows for each table."
        )
    else:
        prompt = (
            "Extract tabular data from this PDF and return it as CSV text. "
            "Return ONLY CSV (no markdown). If there are multiple tables, separate them with a blank line."
        )

    r = _gemini_pdf_model.generate_content(
        [
            prompt,
            {"mime_type": "application/pdf", "data": pdf_bytes},
        ]
    )

    body = _strip_code_fences(getattr(r, "text", "") or "")
    return {
        "format": output_format,
        "data": body,
        "body_url": "",
    }


# ── Convenience: name extraction ─────────────────────────────────────────────


def extract_name_from_text(text: str) -> dict[str, Any]:
    """Extract a best-effort person name from free-form text using Gemini."""
    if not text or not text.strip():
        return {"full_name": "", "confidence": 0.0}
    if not _gemini_text_model:
        raise RuntimeError(
            "Gemini is not configured. Set GEMINI_API_KEY (or GOOGLE_API_KEY)."
        )

    prompt = (
        "You are extracting a person's name from document text. "
        "Return ONLY valid JSON (no markdown) with keys: full_name, confidence. "
        "Rules: "
        "- full_name must be the best single candidate for the person's full name. "
        "- If no name is present, set full_name to empty string and confidence to 0. "
        "- confidence is a number 0 to 1."
        "\n\nTEXT:\n" + text[:12000]
    )

    r = _gemini_text_model.generate_content(prompt)
    data = _parse_json_from_model(getattr(r, "text", "") or "")
    if not data:
        return {"full_name": "", "confidence": 0.0}

    full_name = str(data.get("full_name", "") or "").strip()
    try:
        confidence = float(data.get("confidence", 0.0) or 0.0)
    except Exception:
        confidence = 0.0

    confidence = max(0.0, min(1.0, confidence))
    return {"full_name": full_name, "confidence": confidence}
