from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from sincategorematico_bot import linkedin as linkedin_module
from sincategorematico_bot.linkedin import (
    API_VERSION,
    API_VERSION_ENV,
    LinkedInClient,
    LinkedInError,
    MEMBER_SCOPES,
    PostResult,
    authorization_url,
    escape_commentary,
    linkedin_credentials_usable,
    normalize_post_reference,
    post_url,
    resolve_api_version,
)


class CommentaryTests(unittest.TestCase):
    def test_metacharacters_are_escaped(self) -> None:
        self.assertEqual(escape_commentary("(hola) [mundo]"), r"\(hola\) \[mundo\]")

    def test_hashtags_survive_intact(self) -> None:
        self.assertEqual(escape_commentary("Novedades #IA hoy"), "Novedades #IA hoy")

    def test_plain_text_is_untouched(self) -> None:
        self.assertEqual(escape_commentary("Un texto normal, con coma."), "Un texto normal, con coma.")


class AuthorizationTests(unittest.TestCase):
    def test_the_url_carries_scopes_and_state(self) -> None:
        url = authorization_url(
            client_id="abc",
            redirect_uri="http://localhost:8770/callback",
            scopes=MEMBER_SCOPES,
            state="xyz",
        )
        self.assertIn("client_id=abc", url)
        self.assertIn("state=xyz", url)
        self.assertIn("w_member_social", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8770%2Fcallback", url)

    def test_post_url_is_empty_without_urn(self) -> None:
        self.assertEqual(post_url(""), "")
        self.assertIn("urn:li:share:1", post_url("urn:li:share:1"))

    def test_post_reference_accepts_linkedin_urns_and_public_urls_only(self) -> None:
        self.assertEqual(
            normalize_post_reference("urn:li:share:123456"),
            "urn:li:share:123456",
        )
        self.assertEqual(
            normalize_post_reference(
                "https://www.linkedin.com/feed/update/urn:li:activity:987654/"
            ),
            "urn:li:activity:987654",
        )
        self.assertEqual(
            normalize_post_reference(
                "https://www.linkedin.com/posts/alguien_tema-activity-1234567890-abcd"
            ),
            "urn:li:activity:1234567890",
        )
        self.assertIsNone(
            normalize_post_reference(
                "https://example.test/feed/update/urn:li:activity:987654/"
            )
        )


class ConfigurationTests(unittest.TestCase):
    def test_the_current_default_api_version_is_used(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            client = LinkedInClient("token")
        self.assertEqual(API_VERSION, "202608")
        self.assertEqual(client._headers()["LinkedIn-Version"], "202608")

    def test_a_valid_environment_override_is_used(self) -> None:
        with mock.patch.dict(os.environ, {API_VERSION_ENV: "202701"}, clear=True):
            client = LinkedInClient("token")
        self.assertEqual(client._headers()["LinkedIn-Version"], "202701")

    def test_invalid_versions_are_rejected(self) -> None:
        for value in ("20267", "202613", "latest", "2026-07"):
            with self.subTest(value=value), self.assertRaises(LinkedInError):
                resolve_api_version(value, environ={})


class CredentialValidationTests(unittest.TestCase):
    def usable(self, **overrides: object) -> bool:
        values: dict[str, object] = {
            "access_token": "token",
            "author_urn": "urn:li:person:abc",
            "expires_at": 2_000,
            "scope": "openid profile w_member_social",
            "now": 1_000,
        }
        values.update(overrides)
        return linkedin_credentials_usable(**values)  # type: ignore[arg-type]

    def test_a_person_requires_member_scope(self) -> None:
        self.assertTrue(self.usable())
        self.assertFalse(self.usable(scope="openid profile"))

    def test_an_organization_requires_organization_scope(self) -> None:
        self.assertTrue(
            self.usable(
                author_urn="urn:li:organization:123",
                scope="openid,w_organization_social",
            )
        )
        self.assertFalse(
            self.usable(
                author_urn="urn:li:organization:123",
                scope="openid w_member_social",
            )
        )

    def test_missing_expired_or_malformed_credentials_are_rejected(self) -> None:
        self.assertFalse(self.usable(access_token=""))
        self.assertFalse(self.usable(access_token=" token"))
        self.assertFalse(self.usable(expires_at=1_000))
        self.assertFalse(self.usable(author_urn="urn:li:company:123"))
        self.assertFalse(self.usable(author_urn="urn:li:person:"))
        self.assertFalse(self.usable(author_urn=" urn:li:person:abc"))


class CreatePostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = LinkedInClient("token-de-prueba")

    def test_an_empty_token_is_rejected(self) -> None:
        with self.assertRaises(LinkedInError):
            LinkedInClient("")

    def test_the_payload_carries_the_article_and_the_urn_returns(self) -> None:
        with mock.patch.object(
            linkedin_module, "_request", return_value=(201, {"x-restli-id": "urn:li:share:5"}, "")
        ) as request:
            urn = self.client.create_post(
                author_urn="urn:li:person:abc",
                commentary="Texto (con) paréntesis",
                link="https://ejemplo.test/uno",
                link_title="Titular",
            )
        self.assertEqual(urn, "urn:li:share:5")
        self.assertIsInstance(urn, PostResult)
        self.assertFalse(urn.ambiguous)
        payload = json.loads(request.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["author"], "urn:li:person:abc")
        self.assertEqual(payload["commentary"], r"Texto \(con\) paréntesis")
        self.assertEqual(payload["content"]["article"]["source"], "https://ejemplo.test/uno")

    def test_a_rejected_attachment_falls_back_to_the_link_in_the_text(self) -> None:
        responses = [
            LinkedInError("adjunto inválido", status=422),
            (201, {"x-restli-id": "urn:li:share:6"}, ""),
        ]

        def fake_request(*_args: object, **_kwargs: object):
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(linkedin_module, "_request", side_effect=fake_request):
            urn = self.client.create_post(
                author_urn="urn:li:person:abc",
                commentary="Texto",
                link="https://ejemplo.test/uno",
            )
        self.assertEqual(urn, "urn:li:share:6")

    def test_a_success_without_urn_is_preserved_as_ambiguous_and_not_an_error(self) -> None:
        with mock.patch.object(linkedin_module, "_request", return_value=(201, {}, "{}")):
            result = self.client.create_post(
                author_urn="urn:li:person:abc",
                commentary="Texto",
            )
        self.assertIsInstance(result, PostResult)
        self.assertEqual(result, "")
        self.assertFalse(result)
        self.assertTrue(result.ambiguous)
        self.assertEqual(result.status, 201)

    def test_a_json_null_identifier_is_not_converted_into_the_word_none(self) -> None:
        with mock.patch.object(
            linkedin_module, "_request", return_value=(201, {}, '{"id": null}')
        ):
            result = self.client.create_post(
                author_urn="urn:li:person:abc",
                commentary="Texto",
            )
        self.assertEqual(result, "")
        self.assertTrue(result.ambiguous)

    def test_an_authentication_error_is_flagged(self) -> None:
        error = LinkedInError("token caducado", status=401)
        self.assertTrue(error.is_authentication_problem)
        with mock.patch.object(linkedin_module, "_request", side_effect=error):
            with self.assertRaises(LinkedInError):
                self.client.create_post(author_urn="urn:li:person:abc", commentary="Texto")


class RequestErrorTests(unittest.TestCase):
    def http_error(self, status: int, *, retry_after: str | None = None) -> HTTPError:
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        return HTTPError(
            POSTS_URL_FOR_TEST,
            status,
            "error",
            headers,
            io.BytesIO(b'{"message":"fallo"}'),
        )

    def test_retry_after_is_exposed_from_an_http_error(self) -> None:
        error = self.http_error(429, retry_after="37")
        with mock.patch.object(linkedin_module, "urlopen", side_effect=error):
            with self.assertRaises(LinkedInError) as caught:
                linkedin_module._request(POSTS_URL_FOR_TEST, method="POST")
        self.assertEqual(caught.exception.retry_after, 37)
        self.assertFalse(caught.exception.ambiguous)

    def test_http_5xx_is_ambiguous(self) -> None:
        with mock.patch.object(
            linkedin_module, "urlopen", side_effect=self.http_error(503, retry_after="4")
        ):
            with self.assertRaises(LinkedInError) as caught:
                linkedin_module._request(POSTS_URL_FOR_TEST, method="POST")
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(caught.exception.retry_after, 4)

    def test_network_and_timeout_errors_are_ambiguous(self) -> None:
        for error in (
            URLError("red caída"),
            TimeoutError("agotado"),
            ConnectionResetError("conexión reiniciada"),
        ):
            with self.subTest(error=error), mock.patch.object(
                linkedin_module, "urlopen", side_effect=error
            ):
                with self.assertRaises(LinkedInError) as caught:
                    linkedin_module._request(POSTS_URL_FOR_TEST, method="POST")
            self.assertTrue(caught.exception.ambiguous)


POSTS_URL_FOR_TEST = "https://api.linkedin.test/rest/posts"


if __name__ == "__main__":
    unittest.main()
