#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from configure_token import ENV_PATH, read_environment, write_environment  # noqa: E402
from instalar_servicios import install_templates  # noqa: E402


def main() -> int:
    values = read_environment(ENV_PATH)
    token = values.get("SINCATEGOREMATICO_DASHBOARD_TOKEN") or secrets.token_urlsafe(32)
    values["SINCATEGOREMATICO_DASHBOARD_TOKEN"] = token
    write_environment(ENV_PATH, values)
    # Las unidades del repositorio son plantillas portables. Instalar un
    # symlink directo dejaria @PROJECT_ROOT@ sin resolver y rompería systemd.
    install_templates(
        services=("sincategorematico-dashboard.service",), desktops=()
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "sincategorematico-dashboard.service"],
        check=True,
    )
    # restart aplica la clave nueva incluso si el panel ya estaba activo.
    subprocess.run(
        ["systemctl", "--user", "restart", "sincategorematico-dashboard.service"],
        check=True,
    )
    print("Panel listo: http://127.0.0.1:8765")
    print(f"Clave privada del panel: {token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
