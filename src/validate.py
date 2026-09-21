"""
Crawlability checks for hunted journals (topic-only).

A PDF passes when:
- HTTP 2xx/3xx (follow redirects), no hard 404/410
- content-type looks like PDF (or URL ends .pdf / download / bitstream)
- first bytes are %PDF
- no Cloudflare block (server header, cf-ray, "just a moment")
- no login wall (redirect to /login, 401 with login form)

No email logic here.
"""
import logging
import re
import requests

logger = logging.getLogger(__name__)

CF_HINTS = ("cloudflare", "just a moment", "attention required", "cf-ray")
LOGIN_HINTS = ("/login", "/signin", "/account/login", "login required")


def _is_cloudflare(resp: requests.Response, head: bytes = b"") -> bool:
    server = (resp.headers.get("server") or "").lower()
    if "cloudflare" in server:
        # Cloudflare in front is not an automatic fail; only fail on challenge/block
        pass
    body_hint = ""
    try:
        body_hint = (resp.text or "")[:2000].lower()
    except Exception:
        try:
            body_hint = head[:2000].decode("latin1", "ignore").lower()
        except Exception:
            body_hint = ""
    if "just a moment" in body_hint and "cloudflare" in body_hint:
        return True
    if resp.status_code in (403, 503) and ("cf-ray" in (resp.headers or {}) or "cloudflare" in body_hint):
        return True
    return False


def _is_login_wall(resp: requests.Response, url: str) -> bool:
    final = str(getattr(resp, "url", url) or url).lower()
    if any(h in final for h in LOGIN_HINTS):
        return True
    if resp.status_code == 401:
        return True
    return False


def probe_pdf(url: str, session=None, timeout: int = 20) -> dict:
    """Light probe: HEAD then ranged GET. Returns {alive, reason}."""
    sess = session or requests.Session()
    try:
        r = sess.head(url, timeout=timeout, allow_redirects=True)
        if r.status_code in (405, 501, 403):
            # fall through to ranged GET
            pass
        elif r.status_code in (404, 410):
            return {"alive": False, "reason": f"http_{r.status_code}"}
        elif r.status_code < 400:
            ctype = (r.headers.get("content-type") or "").lower()
            if "pdf" in ctype or re.search(r"\.pdf|download|bitstream", url, re.I):
                if _is_cloudflare(r):
                    return {"alive": False, "reason": "cloudflare_block"}
                if _is_login_wall(r, url):
                    return {"alive": False, "reason": "login_wall"}
                return {"alive": True, "reason": "head_ok"}
    except Exception as e:
        logger.debug("probe HEAD %s: %s", url[:80], e)

    try:
        headers = {"Range": "bytes=0-200", "User-Agent": "Scholarreach-Hunt/1.0 (topic-only)"}
        r = sess.get(url, timeout=timeout, allow_redirects=True, headers=headers, stream=True)
        if r.status_code in (404, 410):
            return {"alive": False, "reason": f"http_{r.status_code}"}
        if r.status_code not in (200, 206):
            return {"alive": False, "reason": f"http_{r.status_code}"}
        if _is_login_wall(r, url):
            return {"alive": False, "reason": "login_wall"}
        head = b""
        try:
            for chunk in r.iter_content(512):
                head += chunk
                if len(head) >= 512:
                    break
        finally:
            r.close()
        if _is_cloudflare(r, head):
            return {"alive": False, "reason": "cloudflare_block"}
        if head[:4] == b"%PDF" or b"%PDF" in head[:512]:
            return {"alive": True, "reason": "pdf_magic"}
        ctype = (r.headers.get("content-type") or "").lower()
        if "pdf" in ctype:
            return {"alive": True, "reason": "content_type_pdf"}
        return {"alive": False, "reason": "not_pdf"}
    except Exception as e:
        # Network flake: don't permanently reject on one error; caller samples 10
        return {"alive": False, "reason": f"fetch_error:{str(e)[:60]}"}
