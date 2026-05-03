"""
pdf_extractor.py — PDF.co API integration for text extraction.
Supports:
  • extract_text_from_url()    — extract from a remote PDF URL
  • extract_text_from_file()   — upload a local file, then extract
  • extract_text_from_bytes()  — upload raw bytes (FastAPI UploadFile content)

PDF.co docs: https://developer.pdf.co/api/pdf-to-text/index.html
"""

import os
import time
import logging
import requests
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
PDFCO_API_KEY  = os.getenv("PDFCO_API_KEY", "")
PDFCO_BASE_URL = "https://api.pdf.co/v1"

# Maximum polling attempts (1 s interval → 60 s total)
MAX_POLL_ATTEMPTS = 60
POLL_INTERVAL_S   = 1


# ── Helpers ────────────────────────────────────────────────────────────────────

def _headers() -> dict:
    """Return auth headers."""
    if not PDFCO_API_KEY:
        raise RuntimeError(
            "PDFCO_API_KEY is not set. "
            "Add it to your .env file:  PDFCO_API_KEY=your_key_here"
        )
    return {"x-api-key": PDFCO_API_KEY, "Content-Type": "application/json"}


def _poll_job(job_id: str) -> dict:
    """
    Poll the PDF.co async job until it completes or fails.
    Returns the final JSON response.
    """
    url = f"{PDFCO_BASE_URL}/job/check"
    for attempt in range(MAX_POLL_ATTEMPTS):
        resp = requests.get(url, params={"jobid": job_id}, headers=_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "").lower()

        if status == "success":
            return data
        elif status in ("error", "failed", "aborted"):
            raise RuntimeError(f"PDF.co job {job_id} failed: {data.get('message', 'unknown error')}")

        logger.debug(f"PDF.co job {job_id}: status={status}, attempt {attempt + 1}")
        time.sleep(POLL_INTERVAL_S)

    raise TimeoutError(f"PDF.co job {job_id} did not complete within {MAX_POLL_ATTEMPTS} seconds")


def _fetch_result_text(result_url: str) -> str:
    """Download the extracted text from the result URL."""
    resp = requests.get(result_url, timeout=60)
    resp.raise_for_status()
    return resp.text


# ── Upload helper (for local files / bytes) ────────────────────────────────────

def _upload_to_pdfco(filename: str, file_bytes: bytes) -> str:
    """
    Upload a file to PDF.co's temporary storage.
    Returns the temporary URL of the uploaded file.
    """
    # Step 1: Get a presigned upload URL
    presign_resp = requests.get(
        f"{PDFCO_BASE_URL}/file/upload/get-presigned-url",
        params={"name": filename, "encrypt": "true"},
        headers=_headers(),
        timeout=30,
    )
    presign_resp.raise_for_status()
    presign_data = presign_resp.json()

    if presign_data.get("error"):
        raise RuntimeError(f"PDF.co presign error: {presign_data.get('message')}")

    upload_url  = presign_data["presignedUrl"]
    file_url    = presign_data["url"]   # The permanent reference URL for extraction

    # Step 2: PUT the file bytes to the presigned URL
    put_resp = requests.put(
        upload_url,
        data=file_bytes,
        headers={"Content-Type": "application/octet-stream"},
        timeout=120,
    )
    put_resp.raise_for_status()

    logger.info(f"Uploaded '{filename}' to PDF.co temporary storage → {file_url}")
    return file_url


# ── Core extraction functions ──────────────────────────────────────────────────

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
        body_url   - PDF.co result URL (if inline_result=False)
    """
    payload = {
        "url":    pdf_url,
        "lang":   lang,
        "ocr":    ocr_enabled,
        "pages":  pages,
        "async":  True,           # Use async so large files don't time out
        "inline": inline_result,
    }

    logger.info(f"Requesting PDF.co text extraction for URL: {pdf_url}")
    resp = requests.post(
        f"{PDFCO_BASE_URL}/pdf/convert/to/text",
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("error"):
        raise RuntimeError(f"PDF.co extraction error: {data.get('message', str(data))}")

    # Handle async job
    job_id = data.get("jobId") or data.get("job_id")
    if job_id:
        data = _poll_job(job_id)

    # Get the result text
    if inline_result and data.get("body"):
        text = data["body"]
    elif data.get("url"):
        text = _fetch_result_text(data["url"])
    else:
        text = ""

    return {
        "text":       text.strip(),
        "page_count": data.get("pageCount", 0),
        "source_url": pdf_url,
        "body_url":   data.get("url", ""),
    }


def extract_text_from_bytes(
    file_bytes: bytes,
    filename: str = "document.pdf",
    pages: str = "",
    lang: str = "eng",
    ocr_enabled: bool = True,
) -> dict:
    """
    Upload PDF bytes to PDF.co and extract text.
    Use this for FastAPI UploadFile content.

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
    temp_url = _upload_to_pdfco(filename, file_bytes)
    return extract_text_from_url(
        pdf_url=temp_url,
        pages=pages,
        lang=lang,
        ocr_enabled=ocr_enabled,
    )


def extract_text_from_file(
    file_path: str | Path,
    pages: str = "",
    lang: str = "eng",
    ocr_enabled: bool = True,
) -> dict:
    """
    Read a local PDF file and extract text via PDF.co.

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
    Extract tables / structured data from a PDF.
    output_format: "json" for structured dict, "csv" for tabular text.
    """
    endpoint = (
        f"{PDFCO_BASE_URL}/pdf/convert/to/json"
        if output_format == "json"
        else f"{PDFCO_BASE_URL}/pdf/convert/to/csv"
    )
    payload = {"url": pdf_url, "async": True, "inline": True}
    resp = requests.post(endpoint, json=payload, headers=_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("error"):
        raise RuntimeError(f"PDF.co structured extract error: {data.get('message')}")

    job_id = data.get("jobId") or data.get("job_id")
    if job_id:
        data = _poll_job(job_id)

    body = data.get("body", "")
    return {
        "format":   output_format,
        "data":     body,
        "body_url": data.get("url", ""),
    }


# ── Convenience: check remaining API credits ───────────────────────────────────

def get_api_credits() -> dict:
    """Return remaining PDF.co API credits for this key."""
    resp = requests.get(
        f"{PDFCO_BASE_URL}/account/credit/balance",
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
