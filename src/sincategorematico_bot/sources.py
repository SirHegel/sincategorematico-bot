from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
from html.parser import HTMLParser
import html as html_module
import http.client
import io
import ipaddress
import re
import socket
import zlib
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "sincategorematico-bot/0.2 (+local)"

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

ATOM = "{http://www.w3.org/2005/Atom}"
DUBLIN_CORE = "{http://purl.org/dc/elements/1.1/}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
RSS10 = "{http://purl.org/rss/1.0/}"


class FeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedEntry:
    guid: str
    url: str
    title: str
    summary: str
    published_at: int | None


@dataclass(frozen=True)
class FeedResult:
    entries: list[FeedEntry]
    etag: str | None
    modified: str | None
    not_modified: bool = False


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in {"p", "br", "div", "li"}:
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)


def strip_html(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.chunks)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _url_target(url: str) -> tuple[SplitResult, str, int]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError):
        raise FeedError("La dirección del feed no es válida") from None

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise FeedError("La dirección del feed no usa http/https")
    if port == 0:
        raise FeedError("El puerto del feed no es válido")
    if parts.username is not None or parts.password is not None:
        raise FeedError("La dirección del feed no puede incluir credenciales")

    host = parts.hostname.lower().rstrip(".")
    if not host or any(character.isspace() or ord(character) < 32 for character in host):
        raise FeedError("El host del feed no es válido")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise FeedError("El host del feed no es público")
    if "%" in host:
        # Los identificadores de zona IPv6 solo tienen sentido en redes locales y
        # además poseen varias representaciones ambiguas según el cliente HTTP.
        raise FeedError("El host del feed no es público")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise FeedError("El host del feed no es válido") from None
    return parts, host, port if port is not None else (443 if scheme == "https" else 80)


def _literal_ip(host: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, bool] | None:
    """Devuelve (IP, notación_ambigua) si *host* representa una IP literal."""
    try:
        return ipaddress.ip_address(host), False
    except ValueError:
        pass

    # inet_aton reconoce las formas históricas explotadas para SSRF: un entero
    # decimal/hexadecimal, octal y direcciones IPv4 abreviadas como 127.1.
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed), True


def _is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Se rechaza toda IPv4 mapeada en IPv6. No aporta nada a un feed público y
    # históricamente permite eludir filtros que solo inspeccionan IPv4 textual.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    return not (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or getattr(address, "is_site_local", False)
    )


def _resolve_public_addresses(host: str, port: int) -> list[tuple[object, ...]]:
    literal = _literal_ip(host)
    if literal is not None:
        address, ambiguous = literal
        if ambiguous or not _is_public_ip(address):
            raise FeedError("El host del feed no es público")

    try:
        addresses = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise FeedError(f"No fue posible resolver el host del feed: {exc}") from None

    if not addresses:
        raise FeedError("El host del feed no devolvió ninguna dirección")

    unique: list[tuple[object, ...]] = []
    seen: set[tuple[object, ...]] = set()
    for candidate in addresses:
        family, socktype, proto, canonname, sockaddr = candidate
        raw_address = str(sockaddr[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            raise FeedError("El DNS del feed devolvió una dirección inválida") from None
        if not _is_public_ip(address):
            raise FeedError("El DNS del feed apunta a una dirección no pública")
        key = (family, socktype, proto, sockaddr)
        if key not in seen:
            seen.add(key)
            unique.append((family, socktype, proto, canonname, sockaddr))
    return unique


def _validated_target(url: str) -> tuple[SplitResult, str, int, list[tuple[object, ...]]]:
    parts, host, port = _url_target(url)
    return parts, host, port, _resolve_public_addresses(host, port)


def is_safe_feed_url(url: str) -> bool:
    try:
        _validated_target(url)
    except FeedError:
        return False
    return True


def parse_timestamp(raw: str | None) -> int | None:
    if not raw:
        return None
    candidate = raw.strip()
    for parser in (_parse_rfc822, _parse_iso8601):
        moment = parser(candidate)
        if moment is not None:
            return int(moment.timestamp())
    return None


def _parse_rfc822(raw: str) -> datetime | None:
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse_iso8601(raw: str) -> datetime | None:
    normalized = raw.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _first_text(element: ElementTree.Element, *paths: str) -> str:
    for path in paths:
        found = element.find(path)
        if found is not None and found.text and found.text.strip():
            return found.text.strip()
    return ""


def _entry_link(element: ElementTree.Element) -> str:
    direct = _first_text(element, "link", f"{RSS10}link")
    if direct.startswith("http"):
        return direct
    candidates = element.findall(f"{ATOM}link") + element.findall("link")
    alternate = ""
    for link in candidates:
        href = (link.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        relation = link.get("rel") or "alternate"
        if relation == "alternate":
            return href
        alternate = alternate or href
    return alternate


def parse_feed(payload: bytes) -> list[FeedEntry]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise FeedError(f"El feed no es XML válido: {exc}") from None

    nodes = (
        root.findall(".//item")
        + root.findall(f".//{RSS10}item")
        + root.findall(f".//{ATOM}entry")
    )
    entries: list[FeedEntry] = []
    for node in nodes:
        title = strip_html(_first_text(node, "title", f"{ATOM}title", f"{RSS10}title"))
        url = _entry_link(node)
        if not title or not url:
            continue
        summary = strip_html(
            _first_text(
                node,
                "description",
                f"{RSS10}description",
                f"{ATOM}summary",
                f"{ATOM}content",
                f"{CONTENT_NS}encoded",
            )
        )
        published = parse_timestamp(
            _first_text(
                node,
                "pubDate",
                f"{DUBLIN_CORE}date",
                f"{ATOM}published",
                f"{ATOM}updated",
                "updated",
            )
        )
        raw_id = _first_text(node, "guid", f"{ATOM}id") or url
        entries.append(
            FeedEntry(
                guid=hashlib.sha256(raw_id.encode("utf-8")).hexdigest(),
                url=url,
                title=title[:300],
                summary=summary[:2000],
                published_at=published,
            )
        )
    return entries


def _connect_pinned(
    addresses: list[tuple[object, ...]], timeout: float | object, source_address: object = None
) -> socket.socket:
    """Conecta solo a una de las direcciones que ya superaron la validación."""
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        connection = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    if last_error is not None:
        raise last_error
    raise OSError("No hay direcciones públicas disponibles para conectar")


def _read_limited(stream: object, maximum: int = MAX_FEED_BYTES) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, maximum + 1 - total))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise FeedError("La respuesta del feed no es binaria")
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise FeedError("El feed supera el tamaño máximo permitido")
    return b"".join(chunks)


def _decode_payload(payload: bytes, content_encoding: str) -> bytes:
    encoding = content_encoding.strip().lower()
    if encoding in {"", "identity"}:
        return payload
    if encoding not in {"gzip", "x-gzip"}:
        raise FeedError(f"Codificación HTTP no permitida: {content_encoding}")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as compressed:
            return _read_limited(compressed)
    except FeedError:
        raise
    except (EOFError, OSError, gzip.BadGzipFile, zlib.error) as exc:
        raise FeedError(f"Respuesta gzip ilegible del feed: {exc}") from None


def _request_once(
    parts: SplitResult,
    host: str,
    port: int,
    addresses: list[tuple[object, ...]],
    headers: dict[str, str],
    timeout: int,
) -> tuple[int, dict[str, str], bytes]:
    connection_type = (
        http.client.HTTPSConnection if parts.scheme.lower() == "https" else http.client.HTTPConnection
    )
    connection = connection_type(host, port=port, timeout=timeout)
    connection._create_connection = (  # type: ignore[attr-defined]
        lambda _address, connection_timeout, source_address=None: _connect_pinned(
            addresses, connection_timeout, source_address
        )
    )
    response: http.client.HTTPResponse | None = None
    try:
        path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = response.status
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        payload = b""
        if 200 <= status < 300:
            # También se limita la representación comprimida para no descargar
            # indefinidamente antes de poder comprobar el tamaño descomprimido.
            payload = _read_limited(response)
        return status, response_headers, payload
    except FeedError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise FeedError(f"No fue posible leer el feed: {exc}") from None
    finally:
        if response is not None:
            response.close()
        connection.close()


def fetch_feed(
    url: str, *, etag: str | None = None, modified: str | None = None, timeout: int = 20
) -> FeedResult:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified

    current_url = url
    visited: set[str] = set()
    redirects = 0
    while True:
        if current_url in visited:
            raise FeedError("El feed contiene un ciclo de redirecciones")
        visited.add(current_url)

        # La resolución se repite para cada salto y sus IP se entregan a la
        # conexión. Así no existe una segunda consulta DNS susceptible a rebinding.
        parts, host, port, addresses = _validated_target(current_url)
        status, response_headers, payload = _request_once(
            parts, host, port, addresses, headers, timeout
        )

        if status == 304:
            return FeedResult(entries=[], etag=etag, modified=modified, not_modified=True)
        if status in REDIRECT_STATUSES:
            location = response_headers.get("location", "").strip()
            if not location:
                raise FeedError(f"HTTP {status}: redirección sin destino")
            if redirects >= MAX_REDIRECTS:
                raise FeedError("El feed superó el máximo de redirecciones")
            current_url = urljoin(current_url, location)
            redirects += 1
            continue
        if not 200 <= status < 300:
            raise FeedError(f"HTTP {status} al leer el feed")

        payload = _decode_payload(payload, response_headers.get("content-encoding", ""))
        return FeedResult(
            entries=parse_feed(payload),
            etag=response_headers.get("etag"),
            modified=response_headers.get("last-modified"),
        )
