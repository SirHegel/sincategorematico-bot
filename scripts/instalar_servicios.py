#!/usr/bin/env python3
"""Instala las unidades y lanzadores locales a partir de plantillas portables."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = PROJECT_ROOT / "deploy"
UNIT_DIR = Path.home() / ".config/systemd/user"
APPLICATION_DIR = Path.home() / ".local/share/applications"

SERVICES = (
    "sincategorematico-bot.service",
    "sincategorematico-engine.service",
    "sincategorematico-dashboard.service",
)
DESKTOPS = (
    "sincategorematico.desktop",
    "sincategorematico-web.desktop",
)
PLACEHOLDERS = {
    "@PROJECT_ROOT@": str(PROJECT_ROOT),
    "@HOME@": str(Path.home()),
}


def force_safe_startup(
    *, state_path: Path | None = None, config_path: Path | None = None
) -> None:
    """Deja el motor pausado y en simulación antes de cualquier reinicio."""

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from sincategorematico_bot.config import load_config
    from sincategorematico_bot.runtime import apply_defaults
    from sincategorematico_bot.storage import StateStore

    selected_state = state_path or Path(
        os.environ.get(
            "SINCATEGOREMATICO_STATE_PATH",
            str(Path.home() / ".local/state/sincategorematico-bot/state.db"),
        )
    )
    selected_config = config_path or PROJECT_ROOT / "config.toml"
    store = StateStore(selected_state)
    try:
        apply_defaults(store, load_config(selected_config))
        store.set("publishing_paused", True)
        store.set("dry_run", True)
        store.add_activity(
            "seguridad", "Instalación preparada en pausa y simulación antes del reinicio"
        )
    finally:
        store.close()


def _safe_replacements(replacements: dict[str, str]) -> None:
    for name, value in replacements.items():
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"La ruta de reemplazo {name} no es segura")


def _systemd_path(value: str) -> str:
    """Escapa una ruta para directivas systemd sin depender de shell quoting."""

    return (
        value.replace("\\", "\\x5c")
        .replace(" ", "\\x20")
        .replace("\t", "\\x09")
        .replace('"', "\\x22")
        .replace("'", "\\x27")
        .replace("%", "%%")
    )


def _desktop_exec_path(value: str) -> str:
    """Escapa una ruta dentro de un argumento citado de ``Exec=``."""

    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )


def _desktop_string(value: str) -> str:
    """Escapa un valor normal del formato Desktop Entry (por ejemplo Icon)."""

    return value.replace("\\", "\\\\")


def _replace_line(source: Path, line: str, substitutions: dict[str, str]) -> str:
    if source.suffix == ".service":
        encode = _systemd_path
    elif source.suffix == ".desktop" and line.startswith("Exec="):
        encode = _desktop_exec_path
    elif source.suffix == ".desktop":
        encode = _desktop_string
    else:
        encode = lambda value: value
    for placeholder, value in substitutions.items():
        line = line.replace(placeholder, encode(value))
    return line


def _installed_mode(source: Path) -> int:
    """Normaliza permisos sin heredar modos accidentales del clon."""

    if source.suffix == ".service":
        return 0o644
    if source.suffix == ".desktop":
        # Algunos lanzadores comprueban el bit ejecutable aunque la
        # especificación Desktop Entry no lo exija en todos los escritorios.
        return 0o755
    return stat.S_IMODE(source.stat().st_mode)


def render_template(
    source: Path,
    target: Path,
    *,
    replacements: dict[str, str] | None = None,
) -> bool:
    """Renderiza ``source`` de forma atómica y aplica un modo Unix canónico.

    Devuelve ``True`` cuando el destino cambió. Un enlace simbólico antiguo se
    reemplaza siempre por un archivo normal para que el repositorio se pueda mover
    o actualizar sin dejar a systemd apuntando a una plantilla sin renderizar.
    """

    if not source.is_file():
        raise FileNotFoundError(f"Falta la plantilla {source}")
    substitutions = PLACEHOLDERS if replacements is None else replacements
    _safe_replacements(substitutions)

    rendered = "".join(
        _replace_line(source, line, substitutions)
        for line in source.read_text(encoding="utf-8").splitlines(keepends=True)
    )
    unresolved = sorted(
        token for token in ("@PROJECT_ROOT@", "@HOME@") if token in rendered
    )
    if unresolved:
        raise ValueError(
            f"La plantilla {source.name} conserva marcadores: {', '.join(unresolved)}"
        )

    payload = rendered.encode("utf-8")
    mode = _installed_mode(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.is_symlink() and target.is_file():
        if target.read_bytes() == payload and stat.S_IMODE(target.stat().st_mode) == mode:
            return False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.fchmod(temporary.fileno(), mode)
        os.replace(temporary_name, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return True


def install_templates(
    *,
    services: tuple[str, ...] = SERVICES,
    desktops: tuple[str, ...] = DESKTOPS,
) -> None:
    """Instala los subconjuntos solicitados; es reutilizable por configuradores."""

    for name in services:
        changed = render_template(DEPLOY / name, UNIT_DIR / name)
        print(f"{'Instalado' if changed else 'Sin cambios'}: {UNIT_DIR / name}")
    for name in desktops:
        changed = render_template(DEPLOY / name, APPLICATION_DIR / name)
        print(f"{'Instalado' if changed else 'Sin cambios'}: {APPLICATION_DIR / name}")


def install_secret_hook() -> int:
    hook = PROJECT_ROOT / ".githooks/pre-commit"
    scanner = PROJECT_ROOT / "tools/scan-secretos.sh"
    if not hook.is_file() or not scanner.is_file():
        print("Falta el hook o el escáner de secretos.", file=sys.stderr)
        return 1
    result = subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode == 0:
        print("Protección pre-commit activada para este clon (.githooks).")
    else:
        print("No fue posible activar el hook pre-commit.", file=sys.stderr)
    return result.returncode


def systemctl(*arguments: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        check=False,
        text=True,
        capture_output=capture,
    )


def activate_services() -> list[str]:
    failures: list[str] = []
    if systemctl("daemon-reload").returncode != 0:
        failures.append("systemctl --user daemon-reload")

    for unit in SERVICES:
        if systemctl("enable", unit).returncode != 0:
            failures.append(f"habilitar {unit}")
        # restart también inicia una unidad inactiva y carga el código actualizado.
        if systemctl("restart", unit).returncode != 0:
            failures.append(f"reiniciar {unit}")

    print("\nEstado de los servicios:")
    for unit in SERVICES:
        result = systemctl("is-active", unit, capture=True)
        state = result.stdout.strip() or "desconocido"
        print(f"  {unit:<42} {state}")
        if result.returncode != 0:
            failures.append(f"comprobar activo {unit} ({state})")
    return failures


def main() -> int:
    if sys.version_info < (3, 11):
        print("Se requiere Python 3.11 o posterior para instalar y ejecutar el bot.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--solo-renderizar",
        action="store_true",
        help="instala unidades y lanzadores, pero no recarga ni inicia systemd",
    )
    parser.add_argument(
        "--instalar-hook",
        action="store_true",
        help="activa el escáner de secretos antes de cada commit en este clon",
    )
    parser.add_argument(
        "--reiniciar",
        action="store_true",
        help="compatibilidad: el comportamiento normal ya reinicia todos los servicios",
    )
    arguments = parser.parse_args()

    if not arguments.solo_renderizar:
        try:
            force_safe_startup()
        except Exception as exc:
            print(f"No fue posible aplicar el arranque seguro: {exc}", file=sys.stderr)
            return 2

    try:
        install_templates()
    except (OSError, ValueError) as exc:
        print(f"No fue posible instalar los archivos: {exc}", file=sys.stderr)
        return 2

    failures: list[str] = []
    if arguments.instalar_hook and install_secret_hook() != 0:
        failures.append("activar el hook de secretos")
    if not arguments.solo_renderizar:
        failures.extend(activate_services())

    if failures:
        print("\nInstalación incompleta:", file=sys.stderr)
        for failure in dict.fromkeys(failures):
            print(f"  - {failure}", file=sys.stderr)
        print(
            "Diagnóstico: journalctl --user -u sincategorematico-engine.service -n 100",
            file=sys.stderr,
        )
        return 1

    if arguments.solo_renderizar:
        print("\nArchivos renderizados; no se ejecutó ningún servicio.")
    else:
        print("\nServicios instalados, reiniciados y activos.")
        print("Registros: journalctl --user -u sincategorematico-engine.service -f")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
