#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sincategorematico_bot.telegram_api import TelegramAPI, TelegramAPIError  # noqa: E402


ENV_PATH = Path.home() / ".config/sincategorematico-bot/bot.env"
TOKEN_KEY = "SINCATEGOREMATICO_TELEGRAM_TOKEN"


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_environment(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = [
        "# Archivo privado de systemd; no compartir ni versionar.",
        *[f"{key}={value}" for key, value in sorted(values.items())],
        "",
    ]
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix="bot.env.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write("\n".join(lines))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Configura el token sin mostrarlo")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="habilita e inicia el servicio después de guardar el token",
    )
    args = parser.parse_args()

    token = getpass.getpass("Pega el token NUEVO de BotFather (entrada oculta): ").strip()
    if not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{30,}", token):
        print("El formato del token no parece válido.", file=sys.stderr)
        return 2

    try:
        bot = TelegramAPI(token).get_me()
    except (TelegramAPIError, ValueError) as exc:
        print(f"Telegram no aceptó el token: {exc}", file=sys.stderr)
        return 3

    values = read_environment(ENV_PATH)
    values[TOKEN_KEY] = token
    write_environment(ENV_PATH, values)
    print(f"Token validado para @{bot.get('username', 'bot')} y guardado con permisos 0600.")

    if args.activate:
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "sincategorematico-bot.service"],
            check=False,
        )
        if result.returncode != 0:
            print("El token se guardó, pero systemd no pudo iniciar el servicio.", file=sys.stderr)
            return result.returncode
        time.sleep(1)
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "sincategorematico-bot.service"],
            check=False,
        )
        if active.returncode != 0:
            print(
                "El servicio fue habilitado, pero no permaneció activo. Revisa journalctl.",
                file=sys.stderr,
            )
            return active.returncode
        print("Servicio habilitado, iniciado y activo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
