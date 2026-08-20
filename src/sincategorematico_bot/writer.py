from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

LOGGER = logging.getLogger(__name__)

MAX_POST_CHARACTERS = 2800
DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_SECONDS = 240

# La CLI exige la clave "mcpServers"; un objeto vacío a secas es rechazado.
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

# La CLI no necesita ver el entorno completo del bot. La lista permite localizar
# el binario, la sesión local de Claude, el locale y autoridades TLS personalizadas;
# todo token o secreto ajeno queda fuera por construcción.
CLAUDE_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "CLAUDE_CONFIG_DIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)

SYSTEM_PROMPT = (
    "Eres el redactor de una cuenta profesional de LinkedIn. Escribes en español neutro, "
    "en primera persona del singular, sin emojis decorativos, sin frases de relleno y sin "
    "promesas comerciales. Nunca inventas datos, cifras ni declaraciones que no estén en la "
    "fuente. Respondes exclusivamente con un objeto JSON válido, sin texto adicional ni "
    "bloques de código."
)

PROMPT_TEMPLATE = """\
Vas a preparar una publicación de LinkedIn a partir de una noticia.

PERFIL EDITORIAL
- Cuenta: {display_name}
- Temas de interés: {topics}
- Audiencia: {audience}
- Tono: {tone}

NOTICIA
- Titular: {title}
- Fuente: {source}
- Enlace: {link}
- Resumen disponible: {summary}

REGLAS
1. Si la noticia no encaja con los temas de interés o no aporta nada a la audiencia,
   devuelve "descartar": true y explica por qué en "motivo".
2. El texto debe tener entre 600 y {max_characters} caracteres.
3. Estructura: una primera línea que funcione como gancho, dos o tres párrafos cortos
   con el aporte propio, y una pregunta final que invite a comentar.
4. No incluyas el enlace dentro del texto: se añade aparte.
5. Entre 2 y 4 hashtags al final, en una sola línea.
6. Solo puedes afirmar lo que se deduce del titular y el resumen. Si el resumen es pobre,
   escribe sobre la implicación general del titular sin inventar detalles.

Responde únicamente con este JSON:
{{"descartar": false, "motivo": "", "post": "texto completo de la publicación"}}
"""


class WriterError(RuntimeError):
    pass


@dataclass(frozen=True)
class Draft:
    body: str
    discarded: bool
    reason: str


@dataclass(frozen=True)
class EditorialProfile:
    display_name: str
    topics: str
    audience: str
    tone: str
    model: str = DEFAULT_MODEL
    max_characters: int = MAX_POST_CHARACTERS


def claude_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Construye el entorno mínimo de Claude sin heredar secretos del proceso."""

    source = os.environ if environ is None else environ
    return {key: source[key] for key in CLAUDE_ENV_ALLOWLIST if key in source}


def extract_json(raw: str) -> dict[str, object]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise WriterError("La respuesta del modelo no contenía JSON") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            raise WriterError("La respuesta del modelo no era JSON válido") from None
    if not isinstance(parsed, dict):
        raise WriterError("La respuesta del modelo no era un objeto JSON")
    return parsed


def normalize_post(raw: str, *, max_characters: int) -> str:
    text = raw.replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    if len(text) > max_characters:
        cut = text[:max_characters]
        boundary = max(cut.rfind("\n\n"), cut.rfind(". "))
        text = cut[: boundary + 1].strip() if boundary > max_characters // 2 else cut.strip()
    return text


class ClaudeWriter:
    """Redacta usando la CLI local de Claude, sin claves de API adicionales."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.executable = executable or shutil.which("claude") or os.path.expanduser(
            "~/.local/bin/claude"
        )
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return bool(self.executable) and os.access(self.executable, os.X_OK)

    def compose(
        self,
        *,
        profile: EditorialProfile,
        title: str,
        summary: str,
        link: str,
        source: str,
    ) -> Draft:
        if not self.available():
            raise WriterError("No se encontró la CLI de Claude para redactar")

        prompt = PROMPT_TEMPLATE.format(
            display_name=profile.display_name,
            topics=profile.topics,
            audience=profile.audience,
            tone=profile.tone,
            title=title,
            source=source or "desconocida",
            link=link,
            summary=summary or "(el feed no incluyó resumen)",
            max_characters=profile.max_characters,
        )
        payload = self._run(prompt, model=profile.model)
        result = extract_json(payload)

        if bool(result.get("descartar")):
            return Draft(body="", discarded=True, reason=str(result.get("motivo", "sin motivo"))[:240])

        body = normalize_post(str(result.get("post", "")), max_characters=profile.max_characters)
        if len(body) < 120:
            raise WriterError("El modelo devolvió un texto demasiado corto")
        return Draft(body=body, discarded=False, reason="")

    def _run(self, prompt: str, *, model: str) -> str:
        environment = claude_environment()
        with tempfile.TemporaryDirectory(prefix="sincategorematico-writer-") as workdir:
            command = [
                self.executable,
                "--safe-mode",
                "-p",
                prompt,
                "--no-session-persistence",
                "--no-chrome",
                "--disable-slash-commands",
                "--model",
                model,
                "--output-format",
                "json",
                "--tools",
                "",
                "--allowed-tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                EMPTY_MCP_CONFIG,
                "--system-prompt",
                SYSTEM_PROMPT,
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    cwd=workdir,
                    env=environment,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise WriterError("La redacción superó el tiempo máximo") from None
            except OSError as exc:
                raise WriterError(f"No fue posible ejecutar la CLI de Claude: {exc}") from None

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise WriterError(f"La CLI de Claude falló: {detail[-1][:200] if detail else 'sin detalle'}")

        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise WriterError("La CLI de Claude no devolvió JSON") from None

        if envelope.get("is_error"):
            raise WriterError(f"La CLI de Claude devolvió un error: {envelope.get('subtype', '')}")
        text = envelope.get("result")
        if not isinstance(text, str) or not text.strip():
            raise WriterError("La CLI de Claude devolvió una respuesta vacía")
        return text
