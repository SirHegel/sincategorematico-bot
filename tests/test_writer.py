from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest import mock

from sincategorematico_bot.writer import (
    ClaudeWriter,
    EditorialProfile,
    SYSTEM_PROMPT,
    WriterError,
    claude_environment,
    extract_json,
    normalize_post,
)

PROFILE = EditorialProfile(
    display_name="Prueba",
    topics="tecnología",
    audience="directivos",
    tone="directo",
)


class WriterParsingTests(unittest.TestCase):
    def test_plain_json_is_read(self) -> None:
        self.assertEqual(extract_json('{"post": "hola"}'), {"post": "hola"})

    def test_fenced_json_is_unwrapped(self) -> None:
        raw = 'Claro:\n```json\n{"post": "hola"}\n```\n'
        self.assertEqual(extract_json(raw), {"post": "hola"})

    def test_json_surrounded_by_prose_is_rescued(self) -> None:
        self.assertEqual(extract_json('Aquí tienes: {"post": "hola"} listo'), {"post": "hola"})

    def test_a_response_without_json_is_rejected(self) -> None:
        with self.assertRaises(WriterError):
            extract_json("No he podido redactar nada.")

    def test_a_json_list_is_rejected(self) -> None:
        with self.assertRaises(WriterError):
            extract_json("[1, 2, 3]")

    def test_long_posts_are_cut_on_a_sentence_boundary(self) -> None:
        text = ("Primera frase larga que ocupa espacio. " * 10) + "Cola sobrante"
        cut = normalize_post(text, max_characters=200)
        self.assertLessEqual(len(cut), 200)
        self.assertTrue(cut.endswith("."))

    def test_blank_lines_are_collapsed(self) -> None:
        self.assertEqual(normalize_post("uno\n\n\n\ndos  \r\n", max_characters=100), "uno\n\ndos")


class FakeWriter(ClaudeWriter):
    """Sustituye la llamada real a la CLI para poder probar el flujo completo."""

    def __init__(self, payload: str) -> None:
        super().__init__(executable="/bin/true")
        self.payload = payload
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def _run(self, prompt: str, *, model: str) -> str:
        self.prompts.append(prompt)
        return self.payload


class WriterComposeTests(unittest.TestCase):
    def compose(self, payload: str):
        writer = FakeWriter(payload)
        return writer, writer.compose(
            profile=PROFILE,
            title="Titular de prueba",
            summary="Resumen de prueba",
            link="https://ejemplo.test/uno",
            source="Fuente",
        )

    def test_a_valid_answer_becomes_a_draft(self) -> None:
        body = "Un texto suficientemente largo para pasar el mínimo. " * 4
        payload = json.dumps({"descartar": False, "motivo": "", "post": body})
        writer, draft = self.compose(payload)
        self.assertFalse(draft.discarded)
        self.assertIn("Titular de prueba", writer.prompts[0])
        self.assertIn("Resumen de prueba", writer.prompts[0])

    def test_a_discarded_news_item_keeps_the_reason(self) -> None:
        payload = json.dumps({"descartar": True, "motivo": "No encaja con los temas"})
        _, draft = self.compose(payload)
        self.assertTrue(draft.discarded)
        self.assertEqual(draft.reason, "No encaja con los temas")
        self.assertEqual(draft.body, "")

    def test_a_too_short_text_is_rejected(self) -> None:
        with self.assertRaises(WriterError):
            self.compose(json.dumps({"descartar": False, "post": "muy corto"}))

    def test_a_missing_cli_is_reported(self) -> None:
        writer = ClaudeWriter(executable="/no/existe/claude")
        self.assertFalse(writer.available())
        with self.assertRaises(WriterError):
            writer.compose(
                profile=PROFILE, title="t", summary="s", link="https://x.test", source="f"
            )


class WriterEnvironmentTests(unittest.TestCase):
    def test_only_the_explicit_allowlist_survives(self) -> None:
        environment = claude_environment(
            {
                "HOME": "/home/prueba",
                "PATH": "/usr/bin",
                "LANG": "es_CO.UTF-8",
                "CLAUDE_CONFIG_DIR": "/config/claude",
                "SSL_CERT_FILE": "/certificados/ca.pem",
                "SINCATEGOREMATICO_TELEGRAM_TOKEN": "test_telegram",
                "DASHBOARD_TOKEN": "test_dashboard",
                "SINCATEGOREMATICO_DASHBOARD_TOKEN": "test_dashboard_2",
                "SINCATEGOREMATICO_LINKEDIN_CLIENT_SECRET": "test_linkedin",
                "ANTHROPIC_API_KEY": "test_anthropic",
                "OPENAI_API_KEY": "test_openai",
                "AWS_SECRET_ACCESS_KEY": "test_aws",
            }
        )
        self.assertEqual(
            environment,
            {
                "HOME": "/home/prueba",
                "PATH": "/usr/bin",
                "LANG": "es_CO.UTF-8",
                "CLAUDE_CONFIG_DIR": "/config/claude",
                "SSL_CERT_FILE": "/certificados/ca.pem",
            },
        )

    def test_the_subprocess_receives_the_sanitized_environment(self) -> None:
        source_environment = {
            "HOME": "/home/prueba",
            "PATH": "/usr/bin",
            "LC_ALL": "C.UTF-8",
            "CLAUDE_CONFIG_DIR": "/config/claude",
            "NODE_EXTRA_CA_CERTS": "/certificados/node.pem",
            "SINCATEGOREMATICO_TELEGRAM_TOKEN": "test_telegram",
            "DASHBOARD_TOKEN": "test_dashboard",
            "LINKEDIN_CLIENT_SECRET": "test_linkedin",
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"is_error": False, "result": '{"post":"texto"}'}),
            stderr="",
        )
        writer = ClaudeWriter(executable="/bin/true")
        with mock.patch.dict(os.environ, source_environment, clear=True), mock.patch(
            "sincategorematico_bot.writer.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(writer._run("instrucción", model="sonnet"), '{"post":"texto"}')
        child_environment = run.call_args.kwargs["env"]
        command = run.call_args.args[0]
        self.assertEqual(child_environment, claude_environment(source_environment))
        self.assertNotIn("SINCATEGOREMATICO_TELEGRAM_TOKEN", child_environment)
        self.assertNotIn("DASHBOARD_TOKEN", child_environment)
        self.assertNotIn("LINKEDIN_CLIENT_SECRET", child_environment)
        for flag in (
            "--safe-mode",
            "--no-session-persistence",
            "--no-chrome",
            "--disable-slash-commands",
            "--strict-mcp-config",
        ):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--allowed-tools") + 1], "")
        self.assertEqual(command[command.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertEqual(command[command.index("--system-prompt") + 1], SYSTEM_PROMPT)
        self.assertNotIn("--append-system-prompt", command)


if __name__ == "__main__":
    unittest.main()
