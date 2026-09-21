"""
PDF title + email extraction.
Uses pypdf for metadata + text, pdfplumber as fallback for better layout text.
"""
import re
import io
import logging
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
from pypdf import PdfReader
import pdfplumber

from src.config import USER_AGENT, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(
    r"""
    (?<![\w.+-])                          # not preceded by word char
    ([a-zA-Z0-9][a-zA-Z0-9._%+-]{0,63}    # local part
    @
    [a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?
    (?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+)
    (?![\w.+-])                           # not followed by word char
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Common noise / false positives to drop
EMAIL_BLACKLIST = {
    "example@example.com",
    "email@example.com",
    "name@domain.com",
    "user@domain.com",
    "info@ijetrm.com",
    "editor@ijetrm.com",
    "ijetrm@gmail.com",
    "support@ijetrm.com",
    "admin@ijetrm.com",
}


def _clean_email(e: str) -> Optional[str]:
    e = e.strip().lower().rstrip(".,;:)")
    if e in EMAIL_BLACKLIST:
        return None
    if len(e) > 80 or len(e) < 6:
        return None
    # drop obvious non-personal journal / system addresses if desired
    if any(x in e for x in ("noreply", "no-reply", "donotreply")):
        return None
    return e


def extract_emails_from_text(text: str) -> List[str]:
    found = set()
    for m in EMAIL_RE.finditer(text or ""):
        cleaned = _clean_email(m.group(1))
        if cleaned:
            found.add(cleaned)
    return sorted(found)


def download_pdf(url: str) -> bytes:
    """
    Download a PDF from a page-sourced URL.
    - Reject relative / non-http URLs immediately.
    - Fail fast on origin 404/410 (no wayback).
    - Other failures: one short Wayback try only (skip guessed OJS paths).
    """
    url = (url or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"Not an absolute PDF URL: {url[:120]!r}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Connection": "close",
    }

    def _try_once(target: str, timeout: int):
        resp = requests.get(
            target,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        )
        if resp.status_code in (404, 410):
            raise FileNotFoundError(f"404 Client Error: NOT FOUND for url: {target}")
        if resp.status_code in (403, 503) and (
            "cloudflare" in (resp.headers.get("server") or "").lower()
            or "just a moment" in (resp.text or "")[:1500].lower()
        ):
            raise RuntimeError(f"Cloudflare blocked PDF {target}")
        resp.raise_for_status()
        data = resp.content
        if data.startswith(b"%PDF"):
            return data
        if b"%PDF" in data[:4000]:
            return data[data.find(b"%PDF") :]
        raise ValueError(
            f"Downloaded content is not a PDF (url={target}, ctype={resp.headers.get('Content-Type')}, len={len(data)})"
        )

    last_err = None
    for attempt in range(2):
        try:
            return _try_once(url, REQUEST_TIMEOUT)
        except FileNotFoundError as e:
            last_err = e
            break
        except Exception as e:
            last_err = e
            import time

            time.sleep(min(4, 2 ** attempt))

    # No Wayback on hard 404 — saves minutes of archive timeouts
    if isinstance(last_err, FileNotFoundError):
        raise last_err

    import re as _re

    looks_guessed = bool(_re.search(r"article/download/\d+(/\d+)?(/pdf)?/?$", url, _re.I))
    if "web.archive.org" not in url and not looks_guessed:
        try:
            return _try_once(f"https://web.archive.org/web/2/{url}", min(20, REQUEST_TIMEOUT + 5))
        except Exception as e:
            last_err = e

    raise last_err if last_err else RuntimeError(f"Failed to download PDF: {url}")



def extract_title_and_emails(data: bytes) -> Tuple[Optional[str], List[str]]:
    """Extract paper title + author emails from PDF bytes."""
    title: Optional[str] = None
    text_parts: List[str] = []

    try:
        reader = PdfReader(io.BytesIO(data))
        meta = reader.metadata
        if meta:
            raw = getattr(meta, "title", None)
            if not raw and hasattr(meta, "get"):
                try:
                    raw = meta.get("/Title")
                except Exception:
                    raw = None
            if raw and str(raw).strip() and str(raw).strip().lower() not in ("untitled", "null"):
                title = str(raw).strip()[:300]
        for page in reader.pages[:5]:
            try:
                text_parts.append(page.extract_text() or "")
            except Exception:
                pass
    except Exception as e:
        logger.debug("pypdf failed: %s", e)

    joined = "\n".join(text_parts)
    if len(joined.strip()) < 80:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages[:4]:
                    try:
                        text_parts.append(page.extract_text() or "")
                    except Exception:
                        pass
            joined = "\n".join(text_parts)
        except Exception as e:
            logger.debug("pdfplumber failed: %s", e)

    if not title:
        for line in joined.splitlines():
            line = line.strip()
            if len(line) >= 12 and "@" not in line and not line.lower().startswith("http"):
                title = line[:300]
                break

    emails = extract_emails_from_text(joined)
    return title, emails


def process_pdf_url(pdf_url: str) -> dict:
    """High-level: download + extract. Returns structured result.

    For OJS article/view URLs, tries article/download variants automatically.
    """
    candidates = [pdf_url]
    # OJS view → download variants
    import re
    m = re.search(r"(article/view/)(\d+)(?:/(\d+))?", pdf_url, re.I)
    if m:
        base = pdf_url[: m.start(1)]
        aid = m.group(2)
        gid = m.group(3)
        candidates = []
        if gid:
            candidates.append(f"{base}article/download/{aid}/{gid}")
        candidates.append(f"{base}article/download/{aid}")
        candidates.append(f"{base}article/download/{aid}/pdf")
        if pdf_url not in candidates:
            candidates.append(pdf_url)
    elif "article/download/" in pdf_url and not pdf_url.rstrip("/").endswith("pdf"):
        candidates = [pdf_url, pdf_url.rstrip("/") + "/pdf"]

    last_err = None
    for cand in candidates:
        try:
            data = download_pdf(cand)
            title, emails = extract_title_and_emails(data)
            return {
                "pdf_url": cand,
                "title": title,
                "emails": emails,
                "email_count": len(emails),
                "source_domain": urlparse(cand).netloc,
            }
        except Exception as e:
            last_err = e
            continue
    raise last_err  # type: ignore
