#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import secrets
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from configure_token import ENV_PATH, read_environment, write_environment  # noqa: E402


def main() -> int:
    code = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    values = read_environment(ENV_PATH)
    values["SINCATEGOREMATICO_CLAIM_SHA256"] = hashlib.sha256(
        code.encode("utf-8")
    ).hexdigest()
    values["SINCATEGOREMATICO_CLAIM_EXPIRES_AT"] = str(int(expires.timestamp()))
    write_environment(ENV_PATH, values)
    print("Código creado. Envíalo al bot antes de 24 horas:")
    print(f"/claim {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
