from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import logging
import math
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)

AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
API_VERSION = "202608"
API_VERSION_ENV = "SINCATEGOREMATICO_LINKEDIN_API_VERSION"
API_VERSION_PATTERN = re.compile(r"^\d{4}(?:0[1-9]|1[0-2])$")
AUTHOR_URN_PATTERN = re.compile(r"^urn:li:(person|organization):[^:\s]+$")
POST_URN_PATTERN = re.compile(r"^urn:li:(?:share|ugcPost|activity):[A-Za-z0-9_-]+$")

MEMBER_SCOPES = ("openid", "profile", "w_member_social")
ORGANIZATION_SCOPES = ("openid", "profile", "w_member_social", "w_organization_social")

# El campo commentary usa el formato "little": estos caracteres son metacaracteres y
# deben escaparse para que aparezcan literales. '#' se deja intacto a propósito para
# que los hashtags sigan funcionando.
LITTLE_SPECIALS = "\\|{}@[]()<>*_~"


class LinkedInError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        retry_after: int | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after
        self.ambiguous = ambiguous

    @property
    def is_authentication_problem(self) -> bool:
        return self.status in {401, 403}


class PostResult(str):
    """URN compatible con ``str`` y metadatos sobre una publicación aceptada.

    LinkedIn puede responder 2xx sin ``x-restli-id`` ni ``id`` en el cuerpo. En ese
    caso el valor es la cadena vacía y ``ambiguous`` es verdadero. Es un resultado
    exitoso pero pendiente de conciliación: el llamador debe conservarlo como
    procesado y no repetir el POST automáticamente, porque podría duplicar el post.
    """

    def __new__(
        cls,
        urn: str = "",
        *,
        ambiguous: bool = False,
        status: int | None = None,
    ) -> PostResult:
        result = super().__new__(cls, urn)
        result.ambiguous = ambiguous
        result.status = status
        return result

    ambiguous: bool
    status: int | None


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    expires_at: int
    refresh_token: str | None
    refresh_expires_at: int | None
    scope: str


def resolve_api_version(
    api_version: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Devuelve una versión YYYYMM explícita, del entorno o la vigente por defecto."""

    environment = os.environ if environ is None else environ
    candidate = (
        api_version
        if api_version is not None
        else environment.get(API_VERSION_ENV, API_VERSION)
    )
    value = str(candidate).strip()
    if not API_VERSION_PATTERN.fullmatch(value):
        raise LinkedInError(
            f"Versión de API de LinkedIn inválida: {value!r}; se esperaba YYYYMM"
        )
    return value


def linkedin_credentials_usable(
    *,
    access_token: str,
    author_urn: str,
    expires_at: int,
    scope: str,
    now: int | None = None,
) -> bool:
    """Valida localmente token, autor, expiración y permiso mínimo para ese autor.

    Es una comprobación pura cuando se pasa ``now`` (útil para motor y pruebas); no
    realiza llamadas de red ni interpreta como válida una URN de tipo desconocido.
    """

    if (
        not isinstance(access_token, str)
        or not access_token
        or any(character.isspace() for character in access_token)
    ):
        return False
    match = AUTHOR_URN_PATTERN.fullmatch(author_urn) if isinstance(author_urn, str) else None
    if match is None:
        return False
    try:
        expiry = int(expires_at)
        current = int(time.time()) if now is None else int(now)
    except (TypeError, ValueError, OverflowError):
        return False
    if expiry <= current:
        return False
    scopes = (
        {item for item in re.split(r"[\s,]+", scope.strip()) if item}
        if isinstance(scope, str)
        else set()
    )
    required_scope = (
        "w_organization_social" if match.group(1) == "organization" else "w_member_social"
    )
    return required_scope in scopes


def escape_commentary(text: str) -> str:
    escaped: list[str] = []
    for character in text:
        if character in LITTLE_SPECIALS:
            escaped.append("\\")
        escaped.append(character)
    return "".join(escaped)


def normalize_post_reference(reference: str) -> str | None:
    """Valida una URN o URL pública de LinkedIn para conciliar un envío."""

    value = reference.strip() if isinstance(reference, str) else ""
    if POST_URN_PATTERN.fullmatch(value):
        return value
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in {"linkedin.com", "www.linkedin.com"}:
        return None
    path = unquote(parsed.path)
    feed_match = re.search(r"/feed/update/(urn:li:(?:share|ugcPost|activity):[A-Za-z0-9_-]+)/?", path)
    if feed_match:
        return feed_match.group(1)
    activity_match = re.search(r"(?:^|[_-])activity-(\d{6,})(?:[-_/]|$)", path)
    if activity_match:
        return f"urn:li:activity:{activity_match.group(1)}"
    return None


def authorization_url(*, client_id: str, redirect_uri: str, scopes: tuple[str, ...], state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes),
        }
    )
    return f"{AUTHORIZATION_URL}?{query}"


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], str]:
    request = Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8", "replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        detail = body
        try:
            parsed = json.loads(body)
            detail = str(parsed.get("message") or parsed.get("error_description") or parsed.get("error") or body)
        except json.JSONDecodeError:
            pass
        retry_after = _parse_retry_after(_header(exc.headers, "Retry-After"))
        raise LinkedInError(
            f"LinkedIn respondió HTTP {exc.code}: {detail[:300]}",
            status=exc.code,
            body=body,
            retry_after=retry_after,
            ambiguous=exc.code == 408 or 500 <= exc.code <= 599,
        ) from None
    except (OSError, HTTPException) as exc:
        raise LinkedInError(
            f"No fue posible conectar con LinkedIn: {getattr(exc, 'reason', exc)}",
            ambiguous=True,
        ) from None


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        direct = headers.get(name)
    except AttributeError:
        direct = None
    if direct is not None:
        return str(direct)
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
    except AttributeError:
        pass
    return None


def _parse_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    raw = value.strip()
    try:
        return max(0, int(raw))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = retry_at.timestamp() - datetime.now(timezone.utc).timestamp()
        return max(0, math.ceil(delay))
    except (TypeError, ValueError, OverflowError):
        return None


def _token_bundle(payload: dict[str, Any]) -> TokenBundle:
    access_token = str(payload.get("access_token", ""))
    if not access_token:
        raise LinkedInError("LinkedIn no devolvió un token de acceso")
    now = int(time.time())
    refresh_expires = payload.get("refresh_token_expires_in")
    return TokenBundle(
        access_token=access_token,
        expires_at=now + int(payload.get("expires_in", 0)),
        refresh_token=payload.get("refresh_token") or None,
        refresh_expires_at=now + int(refresh_expires) if refresh_expires else None,
        scope=str(payload.get("scope", "")),
    )


def exchange_code(
    *, code: str, client_id: str, client_secret: str, redirect_uri: str
) -> TokenBundle:
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    ).encode()
    _, _, payload = _request(
        TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    return _token_bundle(json.loads(payload))


def refresh_token(*, token: str, client_id: str, client_secret: str) -> TokenBundle:
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    _, _, payload = _request(
        TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    return _token_bundle(json.loads(payload))


class LinkedInClient:
    def __init__(self, access_token: str, *, api_version: str | None = None) -> None:
        if not access_token:
            raise LinkedInError("Falta el token de acceso de LinkedIn")
        self._access_token = access_token
        self._api_version = resolve_api_version(api_version)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": self._api_version,
            "Content-Type": "application/json",
        }

    def userinfo(self) -> dict[str, Any]:
        _, _, payload = _request(USERINFO_URL, headers={"Authorization": f"Bearer {self._access_token}"})
        return json.loads(payload)

    def member_urn(self) -> str:
        identifier = str(self.userinfo().get("sub", "")).strip()
        if not identifier:
            raise LinkedInError("LinkedIn no devolvió el identificador del miembro")
        return f"urn:li:person:{identifier}"

    def create_post(
        self,
        *,
        author_urn: str,
        commentary: str,
        link: str | None = None,
        link_title: str = "",
        link_description: str = "",
        visibility: str = "PUBLIC",
    ) -> PostResult:
        payload: dict[str, Any] = {
            "author": author_urn,
            "commentary": escape_commentary(commentary),
            "visibility": visibility,
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if link:
            payload["content"] = {
                "article": {
                    "source": link,
                    "title": (link_title or link)[:200],
                    "description": link_description[:256],
                }
            }

        try:
            return self._post(payload)
        except LinkedInError as exc:
            if link is None or exc.status not in {400, 422}:
                raise
            LOGGER.warning("LinkedIn rechazó el adjunto de enlace; se publica el enlace en el texto")
            fallback = dict(payload)
            fallback.pop("content", None)
            fallback["commentary"] = escape_commentary(f"{commentary}\n\n{link}")
            return self._post(fallback)

    def _post(self, payload: dict[str, Any]) -> PostResult:
        status, headers, body = _request(
            POSTS_URL,
            method="POST",
            headers=self._headers(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        raw_urn: object = headers.get("x-restli-id") or headers.get("X-RestLi-Id") or ""
        urn = raw_urn.strip() if isinstance(raw_urn, str) else ""
        if not urn:
            try:
                raw_urn = json.loads(body).get("id", "")
                urn = raw_urn.strip() if isinstance(raw_urn, str) else ""
            except (json.JSONDecodeError, AttributeError):
                urn = ""
        ambiguous = not bool(urn)
        if ambiguous:
            LOGGER.warning(
                "LinkedIn aceptó la publicación (HTTP %s) pero no devolvió su URN; no se reintentará",
                status,
            )
        return PostResult(urn, ambiguous=ambiguous, status=status)


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/" if urn else ""
