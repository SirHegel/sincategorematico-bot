from __future__ import annotations

import gzip
import io
import socket
import unittest
from unittest import mock

import sincategorematico_bot.sources as sources_module

from sincategorematico_bot.sources import (
    FeedError,
    MAX_FEED_BYTES,
    _connect_pinned,
    _decode_payload,
    _read_limited,
    fetch_feed,
    is_safe_feed_url,
    parse_feed,
    parse_timestamp,
    strip_html,
)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Ejemplo</title>
    <item>
      <title>Una &amp; otra noticia</title>
      <link>https://ejemplo.test/uno</link>
      <description>&lt;p&gt;Resumen con &lt;b&gt;etiquetas&lt;/b&gt;.&lt;/p&gt;</description>
      <pubDate>Tue, 18 Aug 2026 09:30:00 +0000</pubDate>
      <guid>https://ejemplo.test/uno</guid>
    </item>
    <item>
      <title>Sin enlace</title>
      <description>No debería aparecer</description>
    </item>
  </channel>
</rss>
""".encode("utf-8")

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Entrada atom</title>
    <link rel="edit" href="https://ejemplo.test/editar"/>
    <link rel="alternate" href="https://ejemplo.test/dos"/>
    <summary>Resumen breve</summary>
    <updated>2026-08-18T09:30:00Z</updated>
    <id>tag:ejemplo.test,2026:2</id>
  </entry>
</feed>
""".encode("utf-8")


class FeedParsingTests(unittest.TestCase):
    def test_rss_entries_are_cleaned(self) -> None:
        entries = parse_feed(RSS)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.title, "Una & otra noticia")
        self.assertEqual(entry.url, "https://ejemplo.test/uno")
        self.assertEqual(entry.summary, "Resumen con etiquetas.")
        self.assertEqual(entry.published_at, 1787045400)

    def test_atom_prefers_the_alternate_link(self) -> None:
        entry = parse_feed(ATOM)[0]
        self.assertEqual(entry.url, "https://ejemplo.test/dos")
        self.assertEqual(entry.published_at, 1787045400)

    def test_guid_is_hashed_and_stable(self) -> None:
        first, second = parse_feed(RSS)[0], parse_feed(RSS)[0]
        self.assertEqual(first.guid, second.guid)
        self.assertNotIn("ejemplo.test", first.guid)

    def test_script_content_is_discarded(self) -> None:
        self.assertEqual(strip_html("<p>Hola</p><script>alert(1)</script>"), "Hola")

    def test_unknown_dates_are_ignored(self) -> None:
        self.assertIsNone(parse_timestamp("ayer por la tarde"))
        self.assertIsNone(parse_timestamp(None))


class FeedUrlSafetyTests(unittest.TestCase):
    def test_public_https_feeds_are_accepted(self) -> None:
        addresses = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]
        with mock.patch.object(sources_module.socket, "getaddrinfo", return_value=addresses):
            self.assertTrue(is_safe_feed_url("https://ejemplo.test/feed"))

    def test_private_and_local_targets_are_rejected(self) -> None:
        for url in (
            "http://127.0.0.1/feed",
            "http://127.1/feed",
            "http://localhost:8765/api/status",
            "http://10.0.0.5/feed",
            "http://192.168.1.4/feed",
            "http://172.16.3.1/feed",
            "http://169.254.169.254/latest/meta-data",
            "http://224.0.0.1/feed",
            "http://[fec0::1]/feed",
            "http://[ff00::1]/feed",
            "http://impresora.local/feed",
            "http://ejemplo.test:0/feed",
            "http://usuario:clave@ejemplo.test/feed",
            "file:///etc/passwd",
            "ftp://ejemplo.test/feed",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_safe_feed_url(url))

    def test_ambiguous_ipv4_and_mapped_ipv6_cannot_bypass_the_filter(self) -> None:
        for url in (
            "http://2130706433/feed",
            "http://0x7f000001/feed",
            "http://017700000001/feed",
            "http://[::ffff:127.0.0.1]/feed",
            "http://[::ffff:8.8.8.8]/feed",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_safe_feed_url(url))

    def test_dns_private_link_local_and_reserved_results_are_rejected(self) -> None:
        for address in ("10.20.30.40", "169.254.169.254", "192.0.2.10", "127.0.0.1"):
            addresses = [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, 443),
                )
            ]
            with self.subTest(address=address), mock.patch.object(
                sources_module.socket, "getaddrinfo", return_value=addresses
            ):
                self.assertFalse(is_safe_feed_url("https://noticias.example/feed"))

    def test_one_private_dns_answer_rejects_the_whole_target(self) -> None:
        addresses = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.8", 443),
            ),
        ]
        with mock.patch.object(sources_module.socket, "getaddrinfo", return_value=addresses):
            self.assertFalse(is_safe_feed_url("https://noticias.example/feed"))


class FeedFetchingSafetyTests(unittest.TestCase):
    @staticmethod
    def _dns(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        address = "127.0.0.1" if host == "interno.example" else "93.184.216.34"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
        ]

    def test_redirect_target_is_resolved_and_rejected_before_the_next_request(self) -> None:
        with mock.patch.object(
            sources_module.socket, "getaddrinfo", side_effect=self._dns
        ), mock.patch.object(
            sources_module,
            "_request_once",
            return_value=(302, {"location": "http://interno.example/admin"}, b""),
        ) as request_once:
            with self.assertRaisesRegex(FeedError, "no pública"):
                fetch_feed("https://feed.example/rss")
        self.assertEqual(request_once.call_count, 1)

    def test_each_public_redirect_is_fetched_as_a_separate_request(self) -> None:
        responses = [
            (302, {"location": "https://cdn.example/rss.xml"}, b""),
            (200, {"etag": '"v2"'}, RSS),
        ]
        with mock.patch.object(
            sources_module.socket, "getaddrinfo", side_effect=self._dns
        ) as resolver, mock.patch.object(
            sources_module, "_request_once", side_effect=responses
        ) as request_once:
            result = fetch_feed("https://feed.example/rss")

        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.etag, '"v2"')
        self.assertEqual(request_once.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in resolver.call_args_list],
            ["feed.example", "cdn.example"],
        )

    def test_pinned_connection_uses_the_already_validated_socket_address(self) -> None:
        connection = mock.Mock()
        addresses = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]
        with mock.patch.object(sources_module.socket, "socket", return_value=connection):
            result = _connect_pinned(addresses, 12)

        self.assertIs(result, connection)
        connection.settimeout.assert_called_once_with(12)
        connection.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_uncompressed_body_over_four_mib_is_rejected(self) -> None:
        oversized = io.BytesIO(b"x" * (MAX_FEED_BYTES + 1))
        with self.assertRaisesRegex(FeedError, "tamaño máximo"):
            _read_limited(oversized)

    def test_gzip_bomb_is_limited_by_its_decompressed_size(self) -> None:
        compressed = gzip.compress(b"x" * (MAX_FEED_BYTES + 1))
        self.assertLess(len(compressed), MAX_FEED_BYTES)
        with self.assertRaisesRegex(FeedError, "tamaño máximo"):
            _decode_payload(compressed, "gzip")

    def test_gzip_payload_at_the_limit_is_allowed(self) -> None:
        raw = b"x" * MAX_FEED_BYTES
        self.assertEqual(_decode_payload(gzip.compress(raw), "gzip"), raw)


if __name__ == "__main__":
    unittest.main()
