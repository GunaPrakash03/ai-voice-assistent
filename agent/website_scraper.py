"""Read a law firm's website for onboarding: the pages about the firm, its attorneys and practice areas.

``read_site(url)`` opens the homepage, follows up to MAX_PAGES same-site links that look like About /
Attorneys / Team / Practice areas pages, and returns each page as plain text. ``extract_firm_details``
(agent/website_details.py) turns that text into the firm profile's about / practice / attorney fields.

The server fetches an address a user typed, so every request is fenced in:
  * http/https only, default ports or 80/443/8080/8443, no credentials in the URL;
  * the host must resolve only to public addresses, and the connection goes to the address that was
    checked (no second DNS lookup an attacker could swap), on the first request and on every redirect;
  * at most MAX_REDIRECTS redirects, MAX_BYTES per page, HTML only, TOTAL_SECONDS for the whole read;
  * robots.txt is honoured for our user agent.
Standard library only.
"""

import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
import urllib.robotparser
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

log = logging.getLogger("voice-agent.website")

USER_AGENT = "CallDeskBot/1.0 (+firm onboarding; reads public firm pages once)"
MAX_PAGES = 8                 # homepage included
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 4
PAGE_TIMEOUT = 8.0
TOTAL_SECONDS = 25.0
PAGE_TEXT_LIMIT = 12_000
ALLOWED_PORTS = (80, 443, 8080, 8443)

# Which links are worth opening, and how many of each kind.
KINDS = (
    ("about", ("about", "our-firm", "the-firm", "firm-overview", "who-we-are", "our-story", "history", "overview"), 2),
    ("attorneys", ("attorney", "lawyer", "our-team", "team", "people", "professionals", "partners", "staff", "bio", "counsel"), 3),
    ("practice", ("practice", "service", "areas", "what-we-do", "expertise", "cases-we-handle"), 3),
)
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp[34]|mov|ics|xml|json|css|js)$", re.I)


class WebsiteReadError(Exception):
    """The site could not be read; the message is safe to show the admin."""


# ── Safe fetching ────────────────────────────────────────────────────────────
def _public_ip(host: str, port: int) -> str:
    """Resolve ``host`` and return one address, refusing if ANY address is not public."""
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
        raise WebsiteReadError("That address points to a private network and can't be read.")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise WebsiteReadError(f"Couldn't find the website {host}. Check the address.")
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise WebsiteReadError("That address points to a private network and can't be read.")
        addrs.append(str(ip))
    if not addrs:
        raise WebsiteReadError(f"Couldn't find the website {host}. Check the address.")
    return addrs[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, ip, **kw):
        super().__init__(host, **kw)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip, **kw):
        super().__init__(host, context=ssl.create_default_context(), **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _check_url(url: str) -> Tuple[str, str, int, str]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise WebsiteReadError("Only http:// and https:// websites can be read.")
    if parts.username or parts.password:
        raise WebsiteReadError("Enter the website address without a username or password.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise WebsiteReadError("Enter a valid website address, e.g. https://yourfirm.com")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise WebsiteReadError("Enter a valid website address, e.g. https://yourfirm.com")
    if port not in ALLOWED_PORTS:
        raise WebsiteReadError("That website uses an unusual port and can't be read.")
    try:
        ipaddress.ip_address(host)
        raise WebsiteReadError("Enter the firm's website name (e.g. yourfirm.com), not an IP address.")
    except ValueError:
        pass
    path = parts.path or "/"
    return scheme, host, port, path + (f"?{parts.query}" if parts.query else "")


def fetch(url: str, deadline: float, accept_html_only: bool = True) -> Tuple[str, str]:
    """GET ``url`` safely. Returns (final_url, body text). Raises WebsiteReadError."""
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, target = _check_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            raise WebsiteReadError("The website took too long to answer.")
        ip = _public_ip(host, port)
        cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
        conn = cls(host, ip, port=port, timeout=min(PAGE_TIMEOUT, remaining))
        try:
            conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.8",
                                                 "Accept-Language": "en", "Connection": "close"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    raise WebsiteReadError("The website sent a broken redirect.")
                url = urljoin(url, loc)
                continue
            if resp.status >= 400:
                raise WebsiteReadError(f"The website answered with an error ({resp.status}).")
            ctype = (resp.getheader("Content-Type") or "").lower()
            if accept_html_only and "html" not in ctype:
                raise WebsiteReadError("That address isn't a web page.")
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                body = body[:MAX_BYTES]
            charset = re.search(r"charset=([\w-]+)", ctype)
            try:
                text = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            return url, text
        except WebsiteReadError:
            raise
        except ssl.SSLError:
            raise WebsiteReadError("The website's security certificate isn't valid.")
        except (socket.timeout, TimeoutError):
            raise WebsiteReadError("The website took too long to answer.")
        except (OSError, http.client.HTTPException) as e:
            log.info("Fetch %s failed: %s", url, e)
            raise WebsiteReadError("Couldn't connect to the website.")
        finally:
            conn.close()
    raise WebsiteReadError("The website redirected too many times.")


# ── HTML to text ─────────────────────────────────────────────────────────────
class _PageParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "form", "select", "button"}
    BLOCK = {"p", "div", "section", "article", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "td", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.links: List[Tuple[str, str]] = []
        self.title = ""
        self.description = ""
        self._skip = 0
        self._in_title = False
        self._link: Optional[List[Any]] = None
        self._heading: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
            self.description = self.description or (a.get("content") or "").strip()
        elif tag == "a" and a.get("href"):
            self._link = [a["href"], ""]
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3", "h4") and not self._skip:
            self._heading = tag
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._link:
            self.links.append((self._link[0], " ".join(self._link[1].split())))
            self._link = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        if self._link is not None:
            self._link[1] += data
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(l.split()) for l in "".join(self.parts).split("\n")]
        out, blank = [], False
        for l in lines:
            if l.strip("# "):
                out.append(l)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def parse_page(html: str) -> Dict[str, Any]:
    p = _PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception as e:      # malformed markup: keep what was parsed
        log.debug("HTML parse stopped early: %s", e)
    return {"title": " ".join(p.title.split()), "description": p.description, "text": p.text()[:PAGE_TEXT_LIMIT], "links": p.links}


# ── Choosing pages ───────────────────────────────────────────────────────────
def _same_site(a: str, b: str) -> bool:
    strip = lambda h: (h or "").lower().removeprefix("www.")
    return strip(urlsplit(a).hostname) == strip(urlsplit(b).hostname)


def _kind(url: str, label: str) -> Optional[str]:
    hay = (urlsplit(url).path + " " + label).lower().replace("_", "-").replace(" ", "-")
    for kind, words, _ in KINDS:
        if any(w in hay for w in words):
            return kind
    return None


def pick_links(base: str, links: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Same-site links worth reading, at most the per-kind quota, in page order. Returns (url, kind)."""
    chosen: List[Tuple[str, str]] = []
    seen = {urlunsplit(urlsplit(base)._replace(fragment="", query="")).rstrip("/")}
    quota = {k: n for k, _, n in KINDS}
    for href, label in links:
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        url = urlunsplit(urlsplit(urljoin(base, href))._replace(fragment=""))
        if not url.startswith(("http://", "https://")) or not _same_site(url, base) or SKIP_EXT.search(urlsplit(url).path):
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        kind = _kind(url, label)
        if not kind or quota[kind] <= 0:
            continue
        seen.add(key)
        quota[kind] -= 1
        chosen.append((url, kind))
        if len(chosen) >= MAX_PAGES - 1:
            break
    return chosen


def _robots(base: str, deadline: float) -> Optional[urllib.robotparser.RobotFileParser]:
    root = urlunsplit(urlsplit(base)._replace(path="/robots.txt", query="", fragment=""))
    try:
        _, text = fetch(root, min(deadline, time.monotonic() + 4), accept_html_only=False)
    except WebsiteReadError:
        return None                      # no robots.txt (or unreachable): reading is allowed
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    return rp


def read_site(url: str, total_seconds: float = TOTAL_SECONDS) -> Dict[str, Any]:
    """Homepage plus the chosen About / Attorneys / Practice pages as text.

    Returns {"url", "pages": [{url, kind, title, description, text}], "skipped": [{url, reason}]}.
    Raises WebsiteReadError when the homepage itself can't be read."""
    deadline = time.monotonic() + total_seconds
    _check_url(url)
    robots = _robots(url, deadline)
    allowed = lambda u: robots is None or robots.can_fetch(USER_AGENT, u)
    if not allowed(url):
        raise WebsiteReadError("The website asks automated readers not to open it (robots.txt).")
    final_url, html = fetch(url, deadline)
    home = parse_page(html)
    pages = [{"url": final_url, "kind": "home", **{k: home[k] for k in ("title", "description", "text")}}]
    skipped = []
    for link, kind in pick_links(final_url, home["links"]):
        if time.monotonic() > deadline - 1:
            skipped.append({"url": link, "reason": "out of time"})
            continue
        if not allowed(link):
            skipped.append({"url": link, "reason": "robots.txt"})
            continue
        try:
            got_url, body = fetch(link, deadline)
        except WebsiteReadError as e:
            skipped.append({"url": link, "reason": str(e)})
            continue
        page = parse_page(body)
        pages.append({"url": got_url, "kind": kind, **{k: page[k] for k in ("title", "description", "text")}})
    return {"url": final_url, "pages": pages, "skipped": skipped}
