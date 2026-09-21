"""
PDF topic + email extraction for the hunt fleet.

Requires BOTH:
  - topic/title (from PDF metadata / first pages)
  - at least one author email

Reuses extractor.download_pdf + extract_emails_from_text.
"""
import io
import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)


def extract_author_name(_text: str = "", _pdf_bytes: bytes = b""):
    # Optional later: citation_author / first-page author block
    return None


def _title_from_text(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if len(s) >= 12 and "@" not in s and not s.lower().startswith("http"):
            return s[:300]
    return "Untitled paper"


def extract_topic_and_emails(pdf_bytes: bytes) -> dict:
    """Return title, topic, emails[], authorName. emails may be empty if none found."""
    title = None
    text = ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        try:
            meta = reader.metadata
            raw = None
            if meta is not None:
                raw = getattr(meta, "title", None)
                if not raw and hasattr(meta, "get"):
                    try:
                        raw = meta.get("/Title")
                    except Exception:
                        raw = None
            if raw and str(raw).strip() and str(raw).strip().lower() not in ("untitled", "null"):
                title = str(raw).strip()[:300]
        except Exception:
            pass
        parts = []
        for page in reader.pages[:5]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                pass
        text = "\n".join(parts)
    except Exception as e:
        logger.debug("pypdf failed: %s", e)

    if len((text or "").strip()) < 80:
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                parts = []
                for page in pdf.pages[:4]:
                    try:
                        parts.append(page.extract_text() or "")
                    except Exception:
                        pass
                text = "\n".join(parts)
        except Exception as e:
            logger.debug("pdfplumber failed: %s", e)

    if not title:
        title = _title_from_text(text)
    topic = re.sub(r"\s+", " ", title or "").strip()[:300] or "Untitled paper"

    from src.extractor import extract_emails_from_text

    emails = extract_emails_from_text(text or "")
    return {
        "title": title,
        "topic": topic,
        "emails": emails,
        "authorName": extract_author_name(text, pdf_bytes),
    }


def extract_topic(pdf_bytes: bytes) -> dict:
    """Back-compat alias — still returns emails in the dict."""
    return extract_topic_and_emails(pdf_bytes)


def process_pdf_topic(pdf_url: str) -> dict:
    """
    Download PDF and extract topic + emails.
    Raises ValueError if no author email is found (required with topic).
    """
    from src.extractor import download_pdf

    data = download_pdf(pdf_url)
    if not data[:4] == b"%PDF" and b"%PDF" not in data[:4000]:
        raise ValueError("not a PDF")
    out = extract_topic_and_emails(data)
    out["pdf_url"] = pdf_url
    if not out.get("emails"):
        raise ValueError("no author email in PDF (topic+email required)")
    return out
