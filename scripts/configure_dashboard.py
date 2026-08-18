#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from configure_token import ENV_PATH, read_environment, write_environment  # noqa: E402


def main() -> int:
    values = read_environment(ENV_PATH)
    token = values.get("SINCATEGOREMATICO_DASHBOARD_TOKEN") or secrets.token_urlsafe(32)
    values["SINCATEGOREMATICO_DASHBOARD_TOKEN"] = token
    write_environment(ENV_PATH, values)
    unit = Path.home() / ".config/systemd/user/sincategorematico-dashboard.service"
    unit.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    unit.unlink(missing_ok=True)
    unit.symlink_to(ROOT / "deploy/sincategorematico-dashboard.service")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "sincategorematico-dashboard.service"], check=True)
    print("Panel listo: http://127.0.0.1:8765")
    print(f"Clave privada del panel: {token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
