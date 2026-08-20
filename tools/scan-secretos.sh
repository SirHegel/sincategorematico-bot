#!/usr/bin/env bash
set -euo pipefail

modo="${1:---staged}"
if [[ "$modo" != "--staged" && "$modo" != "--todo" ]]; then
  echo "Uso: tools/scan-secretos.sh [--staged|--todo]" >&2
  exit 2
fi

raiz="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "No se encontró un repositorio Git." >&2
  exit 2
}
cd "$raiz"

# El analizador solo informa el nombre del archivo. Nunca imprime la línea,
# coincidencia ni valor que podría contener una credencial real.
exec python3 - "$modo" <<'PY'
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


MODE = sys.argv[1]
# Los blobs moderadamente grandes se revisan completos, también si son
# binarios. Por encima de este techo se falla cerrado en vez de omitirlos.
MAX_SCAN_SIZE = 64 * 1024 * 1024
PATTERNS = (
    ("clave privada", re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("token de Telegram", re.compile(rb"(?<![A-Za-z0-9_-])[0-9]{6,15}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")),
    ("token de GitHub", re.compile(rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{30,}(?![A-Za-z0-9])")),
    ("token de OpenAI", re.compile(rb"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("token de Anthropic", re.compile(rb"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("token de Slack", re.compile(rb"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9-])")),
    ("clave de AWS", re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")),
    ("clave de Google", re.compile(rb"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")),
    ("JWT", re.compile(rb"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])")),
    (
        "credencial dentro de URL",
        re.compile(rb"(?i)https?://[^\s/:@]{2,}:[^\s/@]{8,}@"),
    ),
)
CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?im)(?:"
    rb"(?<![A-Z0-9_.-])[\"']?[A-Z0-9_.-]{0,80}"
    rb"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY)"
    rb"[A-Z0-9_.-]{0,80}[\"']?[ \t]*="
    rb"|[\"'][A-Z0-9_.-]{0,80}"
    rb"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY)"
    rb"[A-Z0-9_.-]{0,80}[\"'][ \t]*:"
    rb"|^[ \t]*[A-Z0-9_.-]{0,80}"
    rb"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY)"
    rb"[A-Z0-9_.-]{0,80}[ \t]*:"
    rb")[ \t]*[\"']?(?P<value>[A-Za-z0-9_./+=:-]{16,})"
)


def git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def candidate_paths() -> list[str]:
    if MODE == "--staged":
        result = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    else:
        result = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if result.returncode != 0:
        raise RuntimeError("Git no pudo enumerar los archivos que se deben revisar")
    return [os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]


def content(path: str) -> tuple[bytes | None, str | None]:
    if MODE == "--staged":
        result = git("show", f":{path}")
        if result.returncode != 0:
            return None, "no se pudo leer el contenido preparado"
        data = result.stdout
    else:
        candidate = Path(path)
        if candidate.is_symlink():
            try:
                data = os.fsencode(os.readlink(candidate))
            except OSError:
                return None, "no se pudo leer el enlace"
        elif not candidate.is_file():
            # Un submódulo es un commit, no un blob local que este escáner
            # pueda inspeccionar. Su repositorio debe aplicar el mismo hook.
            return None, None
        else:
            try:
                size = candidate.stat().st_size
                if size > MAX_SCAN_SIZE:
                    return None, "archivo demasiado grande para verificar"
                data = candidate.read_bytes()
            except OSError:
                return None, "no se pudo leer el archivo"
    if len(data) > MAX_SCAN_SIZE:
        return None, "archivo demasiado grande para verificar"
    # Las expresiones operan sobre bytes: un NUL no convierte el blob en una
    # excepción y tampoco permite ocultar una credencial en un binario.
    return data, None


def has_assigned_credential(data: bytes) -> bool:
    """Bloquea valores largos asignados a nombres propios de credenciales."""

    for match in CREDENTIAL_ASSIGNMENT.finditer(data):
        value = match.group("value")
        lowered = value.lower()
        if lowered.startswith((b"http://", b"https://")):
            continue
        # Constantes como ``CLIENT_SECRET_KEY =
        # SINCATEGOREMATICO_LINKEDIN_CLIENT_SECRET`` aparecen en el código y no
        # son credenciales. Exigimos guion bajo para distinguirlas de secretos
        # hexadecimales o alfanuméricos en mayúsculas.
        if b"_" in value and re.fullmatch(rb"[A-Z][A-Z0-9_]+", value):
            continue
        if lowered.startswith(
            (
                b"example",
                b"dummy",
                b"replace",
                b"change_me",
                b"changeme",
                b"placeholder",
                b"sample",
                b"fake_",
                b"test_",
            )
        ):
            continue
        # No se usa una heurística de "entropía" por clases de caracteres:
        # los client secrets reales con frecuencia son hex puro y también se
        # deben detener antes del commit.
        return True
    return False


try:
    paths = candidate_paths()
except RuntimeError as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(2)

findings: list[tuple[str, str]] = []
for path in paths:
    data, read_problem = content(path)
    if read_problem is not None:
        findings.append((path, read_problem))
        continue
    if data is None:
        continue
    for label, pattern in PATTERNS:
        if pattern.search(data):
            findings.append((path, label))
            break
    else:
        if has_assigned_credential(data):
            findings.append((path, "credencial asignada"))

if findings:
    print("Escaneo bloqueado: hay posibles secretos en estos archivos:", file=sys.stderr)
    for path, label in findings:
        print(f"  - {path} ({label})", file=sys.stderr)
    print("No se mostró ningún valor. Retíralo del índice y rota la credencial si era real.", file=sys.stderr)
    raise SystemExit(1)

scope = "archivos preparados para commit" if MODE == "--staged" else "árbol de trabajo versionable"
print(f"Escaneo de secretos superado: {scope} ({len(paths)} archivos considerados).")
PY
