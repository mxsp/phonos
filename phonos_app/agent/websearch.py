"""Optional public-web tools (search + one page), with explicit guard rails.

The assistant's facts about Phonos always come from Phonos.  These tools exist
for external context only: current documentation, model cards, public papers.
``fetch_url`` refuses local, private, link-local and credential-bearing URLs and
checks every redirect, so the assistant cannot be used as an SSRF probe.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "phonos-agent/1.0 (+https://phonos.fyreflysystems.de)"


class WebError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _check_public_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebError("Only http:// and https:// URLs are allowed.")
    if parsed.username or parsed.password:
        raise WebError("URLs with credentials are not allowed.")
    host = parsed.hostname
    if not host:
        raise WebError("The URL has no host.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise WebError(f"Host not found: {host}") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise WebError("Local and private addresses are blocked.")
    if parsed.port and parsed.port not in (80, 443, 8080, 8443):
        # Keep the rule narrow; SearXNG instances on another port can still be
        # used through the search setting, not through arbitrary fetch_url.
        raise WebError("Only the usual web ports are allowed.")
    return url


def _open(url: str, *, accept: str = "text/html,application/xhtml+xml,text/plain") -> tuple[str, str]:
    current = _check_public_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        request = urllib.request.Request(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Language": "en,de;q=0.8",
            },
        )
        try:
            with _OPENER.open(request, timeout=20) as response:
                ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                body = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if not location:
                    raise WebError(f"Redirect {exc.code} without a target.") from exc
                current = _check_public_url(urllib.parse.urljoin(current, location))
                continue
            raise WebError(f"The server answered with HTTP {exc.code}.") from exc
        except Exception as exc:  # noqa: BLE001
            raise WebError(f"Could not fetch the page: {exc}") from exc
        if len(body) > MAX_BYTES:
            raise WebError("The page is larger than 10 MB.")
        charset = "utf-8"
        match = re.search(r"charset=([A-Za-z0-9._-]+)", response.headers.get("Content-Type") or "")
        if match:
            charset = match.group(1)
        try:
            return body.decode(charset, "replace"), ctype
        except LookupError:
            return body.decode("utf-8", "replace"), ctype
    raise WebError("Too many redirects.")


def _plain(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def search(query: str, limit: int = 6) -> dict:
    """DuckDuckGo HTML search without an API key."""
    query = (query or "").strip()[:400]
    if not query:
        raise WebError("The query is empty.")
    limit = max(1, min(int(limit or 6), 12))
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    page, _ = _open(url, accept="text/html")
    results = []
    # DDG's HTML result blocks are stable enough for a small reader; links can
    # be protocol-relative or wrapped in /l/?uddg=.
    pattern = re.compile(
        r'(?is)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>'
    )
    for match in pattern.finditer(page):
        href = html.unescape(match.group(1))
        title = _plain(match.group(2))[:200]
        snippet = _plain(match.group(3))[:400]
        if href.startswith("//"):
            href = "https:" + href
        parsed = urllib.parse.urlparse(href)
        if parsed.path.startswith("/l/"):
            target = urllib.parse.parse_qs(parsed.query).get("uddg")
            if target:
                href = target[0]
        if not href.startswith(("http://", "https://")):
            continue
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= limit:
            break
    if not results:
        # Fall back to the lite endpoint's simpler markup.
        lite_url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
        lite, _ = _open(lite_url, accept="text/html")
        for match in re.finditer(r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', lite):
            href = html.unescape(match.group(1))
            title = _plain(match.group(2))[:200]
            if title and len(title) > 3:
                results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= limit:
                break
    if not results:
        raise WebError("No results were found.")
    return {"query": query, "results": results, "source": "DuckDuckGo"}


def fetch(url: str, max_chars: int = 6000) -> dict:
    max_chars = max(500, min(int(max_chars or 6000), 20000))
    page, ctype = _open(url)
    if ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
        raise WebError(f"This content type cannot be read as text ({ctype}).")
    text = _plain(page)
    return {
        "url": url,
        "content_type": ctype,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def json_summary(value) -> str:
    return json.dumps(value, ensure_ascii=False)[:2000]
