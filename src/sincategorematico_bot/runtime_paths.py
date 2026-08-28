from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_STATE_PATH = Path.home() / ".local/state/sincategorematico-bot/state.db"


class RuntimePathError(RuntimeError):
    """Una unidad intentó sustituir una ruta privada fijada por la instalación."""


def fixed_runtime_path(environment_name: str, expected: Path) -> Path:
    """Acepta solo la ruta canónica que la instalación ya conoce.

    Las unidades systemd escriben estas variables para hacer explícito su
    contrato, no para ofrecer una entrada de archivos arbitraria. Comparar el
    texto y devolver el objeto de ruta conocido evita que una variable de
    entorno comprometida convierta el bot en un lector o escritor genérico.
    Las pruebas y herramientas internas todavía pueden inyectar una ``Path``
    explícita directamente en sus APIs.
    """

    configured = os.environ.get(environment_name)
    if configured is not None and configured != str(expected):
        raise RuntimePathError(
            f"{environment_name} debe ser exactamente la ruta instalada {expected}"
        )
    return expected


def runtime_config_path() -> Path:
    return fixed_runtime_path("SINCATEGOREMATICO_CONFIG_PATH", DEFAULT_CONFIG_PATH)


def runtime_state_path() -> Path:
    return fixed_runtime_path("SINCATEGOREMATICO_STATE_PATH", DEFAULT_STATE_PATH)
