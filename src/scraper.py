"""
Journal page scrapers — generic discovery for any open-access journal.

Supports:
- IJETRM (citation_pdf_url meta)
- OJS (article/view, article/download)
- DSpace / EPrints / handle-based repos
- Direct PDF links
- Article pages that embed or link to PDFs
- Paginated archive / issue listings
- Custom user journals (listingUrls + samplePaperUrls from UserJournal)
"""
import logging
import time
import re
from typing import List, Dict, Any, Optional, Set
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque

import requests
from bs4 import BeautifulSoup

from src.config import USER_AGENT, REQUEST_TIMEOUT, REQUEST_SLEEP

logger = logging.getLogger(__name__)

MAX_PAGES = 400
MAX_DEPTH = 6
MAX_PDFS = 2500

def _seed_priority(url: str) -> int:
    """Lower = crawl first. Prefer current issue / concrete issue view over vague archives."""
    u = (url or "").lower()
    if re.search(r"issue/current|/current/?$", u):
        return 0
    if re.search(r"issue/view/\d+", u):
        return 1
    if re.search(r"issue/archive|past-issues|/archive", u):
        return 2
    if re.search(r"article/view/", u):
        return 3
    return 5



def expand_ojs_archive_issues(html: str, base_url: str) -> Dict[str, List[str]]:
    """
    From an OJS archive / issue TOC page, collect:
      - all issue/view/{id} links (each volume/issue)
      - archive pagination (/issue/archive/2, ?page=2, Next)
      - sequential issue ids between min..max seen (fills gaps without hardcoding)
    This is how we walk the whole journal, not one page.
    """
    soup = BeautifulSoup(html, "lxml")
    origin = _origin(base_url)
    issues: Set[str] = set()
    archive_pages: Set[str] = set()
    issue_ids: Set[int] = set()
    base_prefix = None  # e.g. https://ijcr.info/index.php/journal/issue/view/

    for a in soup.find_all("a", href=True):
        full = _resolve(a["href"], base_url)
        if not full or not _same_site(full, origin):
            continue
        full = full.split("#")[0]
        m = re.search(r"(https?://[^\s]+/issue/view/)(\d+)/?$", full, re.I)
        if m:
            issues.add(m.group(0).rstrip("/"))
            issue_ids.add(int(m.group(2)))
            base_prefix = m.group(1)
            continue
        # OJS archive pagination: /issue/archive/2 or /issue/archive?page=2
        if re.search(r"/issue/archive(/\d+)?/?$", full, re.I) or re.search(
            r"/issue/archive/?\?[^\s]*page=", full, re.I
        ):
            archive_pages.add(full)
            continue
        text = (a.get_text(" ", strip=True) or "").lower()
        rel = " ".join(a.get("rel") or []).lower()
        if ("next" in rel or text in ("next", "›", "»", "older") or "next" in text) and "archive" in full:
            archive_pages.add(full)

    # Fill sequential gaps only within observed min..max (safe, not infinite guess)
    if base_prefix and issue_ids:
        lo, hi = min(issue_ids), max(issue_ids)
        # Cap span so a weird page cannot explode to 100k ids
        if hi - lo <= 500:
            for i in range(lo, hi + 1):
                issues.add(f"{base_prefix}{i}")

    # Also enqueue archive/1..N if we saw archive/2 pattern
    for ap in list(archive_pages):
        m = re.search(r"(https?://[^\s]+/issue/archive)/(\d+)/?$", ap, re.I)
        if m:
            prefix, n = m.group(1), int(m.group(2))
            for i in range(1, n + 3):  # a couple past last seen
                archive_pages.add(f"{prefix}/{i}" if i > 1 else prefix)

    return {
        "issues": sorted(issues, key=lambda u: int(re.search(r"(\d+)$", u).group(1)) if re.search(r"(\d+)$", u) else 0),
        "archive_pages": sorted(archive_pages),
    }


def probe_pdf_url(url: str, session: Optional[requests.Session] = None) -> bool:
    """
    Lightweight liveness check before enqueue.
    Treat 2xx/3xx as alive; 404/410 as dead. Network errors → assume alive (don't drop).
    """
    sess = session or _session()
    try:
        r = sess.head(url, timeout=12, allow_redirects=True)
        if r.status_code in (405, 501, 403):
            r = sess.get(url, timeout=12, allow_redirects=True, stream=True)
            try:
                next(r.iter_content(256), None)
            except Exception:
                pass
            r.close()
        if r.status_code in (404, 410):
            return False
        return r.status_code < 400
    except Exception:
        return True  # don't discard on flaky network



def find_pagination_links(html: str, base_url: str) -> List[str]:
    """
    Smart pagination: collect next/prev and numbered page links from the current listing.
    Covers OJS (?page=N), DSpace (offset=), WordPress, rel=next, and "Next" anchors.
    """
    soup = BeautifulSoup(html, "lxml")
    origin = _origin(base_url)
    found: Set[str] = set()
    out: List[str] = []

    def add(u: Optional[str]):
        if not u or u in found:
            return
        if not _same_site(u, origin) and "web.archive.org" not in u:
            return
        found.add(u)
        out.append(u)

    # rel="next" / rel="prev"
    for a in soup.find_all("a", href=True):
        rel = " ".join(a.get("rel") or []).lower()
        text = (a.get_text(" ", strip=True) or "").lower()
        href = _resolve(a["href"], base_url)
        if not href:
            continue
        if "next" in rel or text in ("next", "›", "»", "older", "next page", "→"):
            add(href)
        elif re.search(r"\bnext\b|next\s*page|older\s*posts", text, re.I):
            add(href)
        # Numbered pages: page=2, /page/2, offset=60, start=60
        if re.search(r"[?&](page|p|pg|currentPage|offset|start|rpp)=\d+", href, re.I):
            add(href)
        if re.search(r"/page/\d+/?$", href, re.I):
            add(href)

    # OJS issue pagination: /issue/view/123/4 (galley-style) already classified as listing
    # Synthesize sequential page=N from current URL if we only have page=1
    try:
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
        parsed = urlparse(base_url)
        qs = parse_qs(parsed.query)
        cur = None
        for key in ("page", "p", "pg", "currentPage"):
            if key in qs and qs[key]:
                try:
                    cur = int(qs[key][0])
                except ValueError:
                    cur = None
                if cur is not None:
                    for n in range(max(1, cur - 1), cur + 8):
                        if n == cur:
                            continue
                        new_qs = dict(qs)
                        new_qs[key] = [str(n)]
                        q = urlencode({k: v[0] if len(v) == 1 else v for k, v in new_qs.items()}, doseq=True)
                        add(urlunparse(parsed._replace(query=q)))
                    break
        # offset-based (DSpace): offset=0,20,40…
        if "offset" in qs and qs["offset"]:
            try:
                off = int(qs["offset"][0])
            except ValueError:
                off = 0
            step = 20
            if "rpp" in qs and qs["rpp"]:
                try:
                    step = int(qs["rpp"][0]) or 20
                except ValueError:
                    step = 20
            for n in range(0, step * 15, step):
                if n == off:
                    continue
                new_qs = dict(qs)
                new_qs["offset"] = [str(n)]
                q = urlencode({k: v[0] if len(v) == 1 else v for k, v in new_qs.items()}, doseq=True)
                add(urlunparse(parsed._replace(query=q)))
    except Exception:
        pass

    return out




def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close",  # avoid sticky broken keep-alives on flaky hosts
        }
    )
    # Limit pool size so 20 workers don't stampede one origin
    adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _is_cloudflare_block(resp: requests.Response) -> bool:
    """True for Cloudflare OR AWS WAF JS challenges (common on AJOL, BMJ, etc.)."""
    status = resp.status_code
    body = (resp.text or "")[:4000].lower()
    headers = {k.lower(): (v or "") for k, v in (resp.headers or {}).items()}

    # AWS WAF challenge (AJOL returns 202 + x-amzn-waf-action: challenge)
    if status in (202, 401, 403, 405, 503):
        if "x-amzn-waf-action" in headers or "awswaf" in body or "challenge.js" in body:
            return True
        if "gokuprops" in body or "aws-waf-token" in body:
            return True
    if status in (403, 503, 429):
        if "just a moment" in body or "cf-mitigated" in headers.get("cf-mitigated", "").lower():
            return True
        if "cloudflare" in body and ("challenge" in body or "enable javascript" in body):
            return True
        if "cf-browser-verification" in body or "attention required" in body:
            return True
    # Empty/tiny challenge shells
    if status in (200, 202) and len(body) < 8000 and (
        "challenge-container" in body or "awswafintegration" in body
    ):
        return True
    return False


def _wayback_url(url: str) -> str:
    """Internet Archive snapshot proxy — bypasses Cloudflare for blocked OA journals."""
    # Prefer most recent available capture
    return f"https://web.archive.org/web/2/{url}"


def _unwrap_wayback(url: str) -> str:
    """Turn https://web.archive.org/web/2025id_/https://isjem.com/... into the original URL."""
    if not url or "web.archive.org" not in url:
        return url
    m = re.search(r"web\.archive\.org/web/\d+(?:id_|if_|[a-z]*_)?/(https?://.+)$", url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"web\.archive\.org/web/\d+/(https?://.+)$", url, re.I)
    if m:
        return m.group(1)
    return url


def _get(url: str, session: Optional[requests.Session] = None, retries: int = 3) -> str:
    """Fetch with retries, Cloudflare detection, and Wayback Machine fallback."""
    sess = session or _session()
    last_err = None
    candidates = [url]
    # If already a wayback URL, don't nest
    if "web.archive.org" not in url:
        candidates.append(_wayback_url(url))

    for target in candidates:
        for attempt in range(retries):
            try:
                resp = sess.get(target, timeout=REQUEST_TIMEOUT + (15 if "web.archive.org" in target else 0), allow_redirects=True)
                if _is_cloudflare_block(resp):
                    logger.warning("Cloudflare block on %s — will try fallback", target)
                    last_err = RuntimeError(f"Cloudflare blocked {target}")
                    break  # try next candidate
                # 202 Accepted is often AWS WAF challenge (does not raise)
                if resp.status_code >= 400 or _is_cloudflare_block(resp):
                    logger.warning("Bot-block HTTP %s on %s — fallback", resp.status_code, target)
                    last_err = RuntimeError(f"Bot-blocked {target} ({resp.status_code})")
                    break
                resp.raise_for_status()
                html = resp.text or ""
                low = html[:3000].lower()
                if (
                    ("just a moment" in low and "enable javascript" in low)
                    or "awswafintegration" in low
                    or "challenge-container" in low
                    or ("gokuprops" in low and "challenge.js" in low)
                ):
                    logger.warning("WAF/CF challenge HTML on %s", target)
                    last_err = RuntimeError(f"WAF challenge {target}")
                    break
                # Strip wayback toolbar noise slightly
                if "web.archive.org" in target:
                    html = re.sub(r"<!--\s*BEGIN WAYBACK TOOLBAR INSERT[\s\S]*?END WAYBACK TOOLBAR INSERT\s*-->", "", html, flags=re.I)
                time.sleep(REQUEST_SLEEP)
                if target != url:
                    logger.info("Fetched via Wayback: %s", url)
                return html
            except Exception as e:
                last_err = e
                wait = min(15, (2 ** attempt) + 0.5)
                logger.warning("Fetch attempt %d/%d failed %s: %s — retry in %.1fs", attempt + 1, retries, target, e, wait)
                time.sleep(wait)
    raise last_err  # type: ignore



def paper_download_candidates(article_url: str) -> List[str]:
    """
    Synthesize direct download URLs from paper view pages.
    IJIRCT: viewPaper.php?paperId=2606017 → download.php?a_pid=2606017
    """
    out = []
    m = re.search(r"(viewpaper\.php)\?([^#]*)", article_url, re.I)
    if m:
        qs = m.group(2)
        pid = None
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.lower() in ("paperid", "paper_id", "id", "pid", "a_pid"):
                    pid = v.strip()
                    break
        if pid:
            base = article_url[: m.start(1)]  # scheme+host+path up to viewPaper.php
            out.append(f"{base}download.php?a_pid={pid}")
            out.append(f"{base}download.php?paperId={pid}")
    return out


def ojs_download_candidates(article_view_url: str) -> List[str]:
    """
    DEPRECATED — do not use in discovery.
    Synthesizing download URLs caused ~94% 404 rates in live benchmarks.
    Prefer extract_pdfs_from_article_page() on the real article HTML.
    Kept only for emergency debugging.
    """
    m = re.search(r"(article/view/)(\d+)(?:/(\d+))?", article_view_url, re.I)
    if not m:
        return []
    base = article_view_url[: m.start(1)]
    article_id = m.group(2)
    galley_id = m.group(3)
    out = [
        f"{base}article/download/{article_id}",
        f"{base}article/view/{article_id}",
    ]
    if galley_id:
        out.insert(0, f"{base}article/download/{article_id}/{galley_id}")
        out.insert(1, f"{base}article/download/{article_id}/{galley_id}/pdf")
    else:
        # common second-path pattern when galley id is unknown
        out.append(f"{base}article/download/{article_id}/pdf")
    return out


def _origin(url: str) -> str:
    try:
        return urlparse(url).scheme + "://" + urlparse(url).netloc
    except Exception:
        return ""


def _resolve(href: str, base: str) -> Optional[str]:
    if not href:
        return None
    href = href.replace("&amp;", "&").strip()
    if href.startswith(("mailto:", "javascript:", "#")):
        return None
    try:
        full = urljoin(base, href)
        full, _ = urldefrag(full)
        return full
    except Exception:
        return None


def _registrable(host: str) -> str:
    """Rough eTLD+1: arpnjournals.com / arpnjournals.org → arpnjournals"""
    host = (host or "").lower().split(":")[0]
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2]  # good enough for most journal hosts
    return host


def _same_site(url: str, origin: str) -> bool:
    try:
        a = urlparse(url).netloc.lower()
        b = urlparse(origin).netloc.lower()
        if a == b:
            return True
        # Allow sibling TLDs of the same journal (e.g. .com archive → .org PDFs)
        return _registrable(a) == _registrable(b) and _registrable(a) != ""
    except Exception:
        return False


def classify_link(url: str, text_hint: str = "") -> Optional[str]:
    """
    Returns 'pdf' | 'article' | 'listing' | None
    Covers OJS, DSpace, EPrints, handle repos, generic patterns.
    """
    u = (url or "").lower()
    t = (text_hint or "").lower()

    # Direct PDF
    if re.search(r"\.pdf(\?|$)", u, re.I):
        if re.search(r"/cert/|certificate", u, re.I):
            return None
        return "pdf"
    # WordPress Download Manager (ISJEM and similar)
    if re.search(r"/download/[^?#]+", u, re.I) and re.search(r"wpdmdl=\d+", u, re.I):
        return "pdf"
    if re.search(r"wpdmdl=\d+", u, re.I):
        return "pdf"
    if re.search(r"/wp-content/uploads/.*\.pdf", u, re.I):
        return "pdf"

    # OJS / download endpoints
    if re.search(r"article/download/|/download/\d+", u, re.I):
        return "pdf"
    if re.search(r"/pdf/", u, re.I) and not re.search(r"journal", u, re.I):
        return "pdf"

    # OJS/AJOL galley under view: /article/view/{articleId}/{galleyId}
    # (returns application/pdf on AJOL and many OJS hosts — NOT the HTML abstract)
    if re.search(r"article/view/\d+/\d+", u, re.I):
        return "pdf"

    # Link text says PDF (AJOL: "download PDF")
    if t.strip() in ("pdf", "download pdf", "view pdf", "full text pdf", "download") and re.search(r"article/view/\d+", u, re.I):
        return "pdf"
    if "pdf" in t and re.search(r"article/view/\d+/\d+", u, re.I):
        return "pdf"

    # DSpace / EPrints bitstream
    if re.search(r"/bitstream/", u, re.I) and not re.search(r"/cert/", u, re.I):
        return "pdf"
    if re.search(r"/xmlui/bitstream/|/jspui/bitstream/", u, re.I):
        return "pdf"

    # Fulltext / files that look like PDFs
    if re.search(r"/fulltext/", u, re.I) and re.search(r"\.(pdf|doc)", u, re.I):
        return "pdf"
    if re.search(r"/files?/\d+", u, re.I) and re.search(r"\.(pdf|doc)", u, re.I):
        return "pdf"

    # Article HTML pages (view with ONLY article id, no galley id)
    if re.search(r"/show-\d+-\d+", u, re.I):
        return "article"
    if re.search(r"article/view/\d+/?$", u, re.I) or re.search(r"article/view/\d+(\?|$)", u, re.I):
        return "article"
    if re.search(r"article/view/", u, re.I):
        # fallback: still article unless matched galley above
        return "article"
    if re.search(r"/articles?/", u, re.I) and not re.search(r"archive|issue|volume|list-", u, re.I):
        return "article"
    if "abstract" in u and re.search(r"view|full", u, re.I):
        return "article"
    # IJIRCT / custom PHP journals
    if re.search(r"viewpaper\.php\?.*paperid=", u, re.I):
        return "article"
    if re.search(r"view[_-]?paper\.php", u, re.I) and re.search(r"id=", u, re.I):
        return "article"
    if re.search(r"download\.php\?.*a_pid=", u, re.I):
        return "pdf"
    if re.search(r"download\.php\?.*paper", u, re.I):
        return "pdf"

    # DSpace / EPrints handles
    if re.search(r"/handle/\d+/\d+", u, re.I):
        return "article"
    if re.search(r"/xmlui/handle/|/jspui/handle/", u, re.I):
        return "article"
    if re.search(r"/eprints?/\d+|/id/eprint/\d+", u, re.I):
        return "article"

    # Generic record / paper patterns
    if re.search(r"/record/\d+", u, re.I) and not re.search(r"archive|issue", u, re.I):
        return "article"
    if re.search(r"/pub/\d+|/paper/\d+", u, re.I):
        return "article"
    if re.search(r"/publication/\d+", u, re.I) and not re.search(r"list|archive", u, re.I):
        return "article"
    if re.search(r"/items?/", u, re.I) and re.search(r"\d+", u) and not re.search(r"archive|listing", u, re.I):
        return "article"
    if re.search(r"/works?/\d+|/node/\d+", u, re.I) and not re.search(r"archive|issue|volume", u, re.I):
        return "article"

    # Listing / archive / issue / volume TOC pages (ARPN, many society journals)
    if re.search(r"issue/(view|archive|current)", u, re.I):
        return "listing"
    if re.search(r"/archive(\.htm|/|$|\?)", u, re.I) or u.rstrip("/").endswith("archive.htm"):
        return "listing"
    if re.search(r"list-\d+", u, re.I):
        return "listing"
    if re.search(r"/issue[s]?/", u, re.I) or re.search(r"/volume[s]?/", u, re.I):
        return "listing"
    # ARPN-style: volume_01_2026.htm, volume_12_2019.html
    if re.search(r"volume[_-]?\d+", u, re.I):
        return "listing"
    if re.search(r"vol[_-]?\d+.*\.(htm|html|php)", u, re.I):
        return "listing"
    if re.search(r"browse|viewall|all-issues|past-issues|current-issue|back.?issues", u, re.I):
        return "listing"
    if re.search(r"publications\.php", u, re.I) and re.search(r"volume=|issue=", u, re.I):
        return "listing"
    if re.search(r"publications\.php", u, re.I):
        return "listing"
    # ISJEM / WordPress issue archives
    if re.search(r"/past-issues/?", u, re.I):
        return "listing"
    if re.search(r"/volume[-_]?\d+", u, re.I) or re.search(r"volume\d+issue\d+", u, re.I):
        return "listing"
    if re.search(r"/current-issue/?", u, re.I):
        return "listing"
    if re.search(r"/special-edition", u, re.I):
        return "listing"
    if re.search(r"contents?\.htm", u, re.I) or re.search(r"toc\.htm", u, re.I):
        return "listing"
    if "issue" in t and ("view" in t or "archive" in t or "list" in t or re.search(r"\d", t)):
        return "listing"
    if re.search(r"^issue\s*\d+", t, re.I) or re.search(r"volume\s*\d+", t, re.I):
        return "listing"

    return None


def extract_links_from_html(html: str, base_url: str) -> List[Dict[str, str]]:
    """Return list of {url, text, kind} from a page."""
    soup = BeautifulSoup(html, "lxml")
    origin = _origin(base_url)
    out = []
    seen: Set[str] = set()

    # Meta citation_pdf_url (IJETRM and many OA journals)
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"citation_pdf_url", re.I)}):
        pdf = (meta.get("content") or "").strip()
        if pdf and pdf not in seen:
            seen.add(pdf)
            out.append({"url": pdf, "text": "", "kind": "pdf"})

    for a in soup.find_all("a", href=True):
        full = _resolve(a["href"], base_url)
        if not full:
            continue
        full = _unwrap_wayback(full)
        if not full or full in seen:
            continue
        # Prefer same-site, but allow absolute PDF on CDNs
        kind = classify_link(full, a.get_text(" ", strip=True))
        if not kind:
            continue
        if kind != "pdf" and not _same_site(full, origin) and not _same_site(full, _unwrap_wayback(base_url)):
            # still allow volume/issue listings for known journal hosts
            if kind == "listing" and re.search(r"volume|issue|past-issues|archive", full, re.I):
                pass
            else:
                continue
        # Skip template / certificate noise downloads
        if kind == "pdf" and re.search(r"manuscript-template|sample-certificate|copyright-form|author.guideline", full, re.I):
            continue
        seen.add(full)
        out.append(
            {
                "url": full,
                "text": a.get_text(" ", strip=True)[:200],
                "kind": kind,
            }
        )
    return out


def extract_pdfs_from_article_page(html: str, base_url: str) -> List[str]:
    """
    From an article HTML page, find PDF links that ACTUALLY appear on the page.
    Never invent article/download/{id}/{galley} paths.
    Priority: citation_pdf_url meta → explicit PDF anchors → OJS galley/download hrefs
    → iframe/embed → buttons labeled PDF/Full Text that point at real hrefs.
    """
    soup = BeautifulSoup(html, "lxml")
    pdfs: List[str] = []
    seen: Set[str] = set()

    def add(u: Optional[str]):
        if not u or u in seen:
            return
        if re.search(r"certificate|/cert/|manuscript-template|copyright-form|author.?guideline", u, re.I):
            return
        seen.add(u)
        pdfs.append(u)

    # 1) High-confidence meta tags (IJETRM, many OA publishers)
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"citation_pdf_url", re.I)}):
        add((meta.get("content") or "").strip())
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"citation_fulltext_html_url", re.I)}):
        # not a PDF — skip
        pass

    # 2) Anchors: classify + text hints (PDF, Full Text, Download)
    for a in soup.find_all("a", href=True):
        full = _resolve(a["href"], base_url)
        if not full:
            continue
        text = a.get_text(" ", strip=True) or ""
        kind = classify_link(full, text)
        if kind == "pdf":
            add(full)
            continue
        # OJS often labels the real galley link as "PDF" even if URL has no .pdf suffix
        if re.search(r"article/download/|/download/\d+|bitstream|galley", full, re.I):
            add(full)
            continue
        if re.search(r"^\s*(pdf|full\s*text|fulltext|download\s*pdf|view\s*pdf)\s*$", text, re.I):
            if re.search(r"download|pdf|bitstream|galley|get|file", full, re.I):
                add(full)

    # 3) iframe / embed / object
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = tag.get("src") or tag.get("data")
        full = _resolve(src, base_url) if src else None
        if full and re.search(r"\.pdf|download|bitstream|galley", full, re.I):
            add(full)

    # 4) data-* attributes some themes use
    for tag in soup.find_all(attrs={"data-pdf": True}):
        add(_resolve(tag.get("data-pdf"), base_url))
    for tag in soup.find_all(attrs={"data-url": True}):
        u = tag.get("data-url") or ""
        if re.search(r"pdf|download|bitstream", u, re.I):
            add(_resolve(u, base_url))

    return pdfs


def _ijetrm_collect_pdfs(html: str, page_url: str) -> List[Dict[str, Any]]:
    """Pull PDF URLs from one IJETRM issue/volume HTML page."""
    soup = BeautifulSoup(html or "", "lxml")
    papers: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def add(pdf: str, title=None, source="meta"):
        pdf = (pdf or "").strip()
        if not pdf or not pdf.lower().startswith("http"):
            return
        if not (pdf.lower().endswith(".pdf") or "/issues/files/" in pdf.lower()):
            if ".pdf" not in pdf.lower():
                return
        if pdf in seen:
            return
        seen.add(pdf)
        papers.append(
            {
                "title": title,
                "authors": None,
                "pdf_url": pdf,
                "doi": None,
                "source": source,
                "page_url": page_url,
            }
        )

    for meta in soup.find_all("meta", attrs={"name": re.compile(r"citation_pdf_url", re.I)}):
        add(meta.get("content") or "", source="meta")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(page_url, href)
        if ".pdf" in full.lower() or "/issues/files/" in full.lower():
            add(full, title=a.get_text(strip=True) or None, source="anchor")
    return papers


def scrape_ijetrm_issue(issue_url: str) -> List[Dict[str, Any]]:
    """
    IJETRM hosts PDFs under /issues/files/*.pdf and citation_pdf_url meta.
    Volume index is /issue/?volume=Month~Year (and volume=current).
    Crawl current + archive volume links so jobs are not stuck on one month.
    """
    # Normalize common typos / bare domain
    u = (issue_url or "").strip()
    if re.search(r"ijertm\.com", u, re.I):  # frequent misspelling
        u = re.sub(r"ijertm\.com", "ijetrm.com", u, flags=re.I)
    if re.match(r"^https?://(www\.)?ijetrm\.com/?$", u, re.I):
        u = "https://ijetrm.com/issue/?volume=current"
    if "ijetrm.com" in u.lower() and "/issue" not in u.lower():
        u = "https://ijetrm.com/issue/?volume=current"

    papers: List[Dict[str, Any]] = []
    seen_pdf: Set[str] = set()
    volumes: List[str] = []
    seen_vol: Set[str] = set()

    def enqueue_vol(v: str):
        v = (v or "").split("#")[0]
        if not v or v in seen_vol:
            return
        if "ijetrm.com" not in v.lower():
            return
        seen_vol.add(v)
        volumes.append(v)

    enqueue_vol(u)
    enqueue_vol("https://ijetrm.com/issue/?volume=current")
    enqueue_vol("https://ijetrm.com/issue/")

    # First pass: open index pages to collect volume= links
    for seed in list(volumes)[:5]:
        try:
            html = _get(seed)
        except Exception as e:
            logger.warning("IJETRM fetch failed %s: %s", seed, e)
            continue
        for p in _ijetrm_collect_pdfs(html, seed):
            if p["pdf_url"] not in seen_pdf:
                seen_pdf.add(p["pdf_url"])
                papers.append(p)
        soup = BeautifulSoup(html or "", "lxml")
        for a in soup.find_all("a", href=True):
            full = urljoin(seed, a["href"]).split("#")[0]
            if "volume=" in full.lower() or re.search(r"/issue/\?volume=", full, re.I):
                enqueue_vol(full)

    # Prefer recent volumes first (current, 2026, 2025, …)
    def vol_key(v: str):
        if "volume=current" in v.lower():
            return (0, "")
        m = re.search(r"volume=([^&]+)", v, re.I)
        return (1, (m.group(1) if m else v))

    ordered = sorted(volumes, key=vol_key)
    # Cap volume pages so one job does not crawl the entire 2017–2026 archive forever
    max_volumes = 24
    for vol in ordered[:max_volumes]:
        if vol in ("https://ijetrm.com/issue/",) and papers:
            continue
        try:
            html = _get(vol)
        except Exception as e:
            logger.warning("IJETRM volume failed %s: %s", vol, e)
            continue
        for p in _ijetrm_collect_pdfs(html, vol):
            if p["pdf_url"] not in seen_pdf:
                seen_pdf.add(p["pdf_url"])
                papers.append(p)
        if len(papers) >= MAX_PDFS:
            break

    logger.info("IJETRM %s → %d PDF links across %d volume seeds", issue_url, len(papers), len(ordered[:max_volumes]))
    return papers[:MAX_PDFS]


def generic_discover(
    seed_urls: List[str],
    *,
    max_pages: int = MAX_PAGES,
    max_depth: int = MAX_DEPTH,
    max_pdfs: int = MAX_PDFS,
) -> List[Dict[str, Any]]:
    """
    BFS crawl from seed URLs. Collects PDF links and follows listing/article
    pages to discover more. Works for OJS, DSpace, EPrints, generic OA journals.
    """
    if not seed_urls:
        return []

    session = _session()
    queue: deque = deque()  # (url, depth)
    visited: Set[str] = set()
    pdfs: List[Dict[str, Any]] = []
    seen_pdfs: Set[str] = set()
    pages_fetched = 0

    # Prefer current issue / issue/view seeds first (benchmark: better PDF yield)
    ordered = sorted([s for s in seed_urls if s], key=_seed_priority)
    for s in ordered:
        queue.append((s, 0))

    while queue and pages_fetched < max_pages and len(pdfs) < max_pdfs:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        try:
            html = _get(url, session)
            pages_fetched += 1
        except Exception as e:
            logger.warning("Fetch failed %s: %s", url, e)
            continue

        links = extract_links_from_html(html, url)

        # --- Progressive issue/volume walk (OJS and similar) ---
        # Archive pages list many Volume X / Issue Y → issue/view/{id}.
        # Expand ALL issues + archive pagination so we do not stop at one TOC page.
        if re.search(r"issue/archive|issue/current|issue/view/\d+|past-issues|/archive", url, re.I):
            expanded = expand_ojs_archive_issues(html, url)
            for iss in expanded["issues"]:
                if iss not in visited:
                    # Listings (issues) get depth+0 so article pages still have budget
                    queue.append((iss, depth))
            for ap in expanded["archive_pages"]:
                if ap not in visited:
                    queue.append((ap, depth))
            if expanded["issues"] or expanded["archive_pages"]:
                logger.info(
                    "Issue-walk %s → %d issues, %d archive pages (queue=%d)",
                    url[-60:],
                    len(expanded["issues"]),
                    len(expanded["archive_pages"]),
                    len(queue),
                )

        # Always pull PDF hrefs that actually appear on this page (article or listing).
        # Never invent /article/download/{id}/{galley} numbers.
        page_kind = classify_link(url)
        if page_kind in ("article", None) or any(l["kind"] == "pdf" for l in links):
            for pdf in extract_pdfs_from_article_page(html, url):
                if pdf not in seen_pdfs:
                    looks_synth = bool(re.search(r"article/download/\d+(/\d+)?(/pdf)?/?$", pdf, re.I))
                    if looks_synth and not probe_pdf_url(pdf, session):
                        logger.debug("Skip dead article PDF %s", pdf)
                        seen_pdfs.add(pdf)
                        continue
                    seen_pdfs.add(pdf)
                    pdfs.append(
                        {
                            "title": None,
                            "authors": None,
                            "pdf_url": pdf,
                            "doi": None,
                            "source": "article_page",
                            "page_url": url,
                        }
                    )

        for link in links:
            kind = link["kind"]
            href = link["url"]
            if kind == "pdf":
                if href not in seen_pdfs:
                    # Probe guessed-looking download paths; skip confirmed 404s early
                    looks_synth = bool(re.search(r"article/download/\d+(/\d+)?(/pdf)?/?$", href, re.I))
                    if looks_synth and not probe_pdf_url(href, session):
                        logger.debug("Skip dead PDF href %s", href)
                        seen_pdfs.add(href)  # don't retry
                    else:
                        seen_pdfs.add(href)
                        pdfs.append(
                            {
                                "title": link.get("text") or None,
                                "authors": None,
                                "pdf_url": href,
                                "doi": None,
                                "source": "link",
                                "page_url": url,
                            }
                        )
            elif kind == "article":
                # NEVER synthesize download IDs — open the article page and extract real PDF hrefs.
                # This is the root fix for mass 404s on OJS (.../download/N/M/pdf guessed wrong).
                if depth < max_depth and href not in visited:
                    queue.append((href, depth + 1))
            elif kind == "listing" and depth < max_depth:
                if href not in visited:
                    queue.append((href, depth + 1))

        # Smart pagination: enqueue next/numbered listing pages from this HTML
        if depth < max_depth:
            for next_url in find_pagination_links(html, url):
                if next_url not in visited:
                    queue.append((next_url, depth + 1))

        if pages_fetched % 10 == 0:
            logger.info(
                "Generic discover: pages=%d pdfs=%d queue=%d",
                pages_fetched,
                len(pdfs),
                len(queue),
            )

    logger.info(
        "Generic discover done: pages=%d pdfs=%d from %d seeds",
        pages_fetched,
        len(pdfs),
        len(seed_urls),
    )
    return pdfs[:max_pdfs]


def discover_papers(url: str) -> List[Dict[str, Any]]:
    """
    Dispatch: IJETRM-specific fast path, otherwise generic crawler.
    """
    domain = urlparse(url).netloc.lower()
    # IJETRM (+ common typo ijertm.com)
    if "ijetrm.com" in domain or "ijertm.com" in domain:
        return scrape_ijetrm_issue(url)
    return generic_discover([url])


def discover_from_seeds(
    listing_urls: List[str],
    pdf_urls: Optional[List[str]] = None,
    sample_paper_urls: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    High-level entry for custom journals and multi-seed discovery.
    Merges direct PDF seeds + crawl results.
    """
    results: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for u in pdf_urls or []:
        if u and u not in seen:
            seen.add(u)
            results.append(
                {
                    "title": None,
                    "authors": None,
                    "pdf_url": u,
                    "doi": None,
                    "source": "seed_pdf",
                    "page_url": u,
                }
            )

    for u in sample_paper_urls or []:
        if not u or u in seen:
            continue
        if re.search(r"\.pdf(\?|$)", u, re.I):
            seen.add(u)
            results.append(
                {
                    "title": None,
                    "authors": None,
                    "pdf_url": u,
                    "doi": None,
                    "source": "sample_pdf",
                    "page_url": u,
                }
            )
            continue

        # Article/view URL: open the page and extract real PDF links only (no guessed download IDs)
        try:
            found = generic_discover([u], max_pages=5, max_depth=2, max_pdfs=20)
            for p in found:
                if p["pdf_url"] not in seen:
                    seen.add(p["pdf_url"])
                    results.append(p)
        except Exception as e:
            logger.warning("Sample paper crawl failed %s: %s", u, e)

    if listing_urls:
        ordered_listings = sorted(listing_urls, key=_seed_priority)
        crawled = generic_discover(ordered_listings, max_pages=MAX_PAGES, max_depth=MAX_DEPTH, max_pdfs=MAX_PDFS)
        for p in crawled:
            if p["pdf_url"] not in seen:
                seen.add(p["pdf_url"])
                results.append(p)

    return results
