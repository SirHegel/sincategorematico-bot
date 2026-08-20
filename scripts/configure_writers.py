#!/usr/bin/env python3
"""Configura cuentas locales de Claude sin leer ni copiar sus credenciales."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Sequence


MAX_ACCOUNTS = 16
ACCOUNT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z", re.ASCII)
MANIFEST_PATH = Path.home() / ".config/sincategorematico-bot/writers.json"
DROP_IN_PATH = (
    Path.home()
    / ".config/systemd/user/sincategorematico-engine.service.d/writer-accounts.conf"
)


@dataclass(frozen=True)
class WriterAccount:
    account_id: str
    config_dir: Path
    shared_lock: Path | None = None


@dataclass(frozen=True)
class ConfigurationResult:
    manifest_changed: bool
    drop_in_changed: bool


def _split_account(value: str) -> tuple[str, str]:
    """Separa ``ID=RUTA`` sin interpretar la ruta ni acceder a su contenido."""

    if "=" not in value:
        raise ValueError("Cada --account debe tener el formato ID=RUTA")
    account_id, raw_path = value.split("=", 1)
    if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise ValueError(
            "ID de cuenta no válido; usa 1-64 caracteres: a-z, 0-9, _ o -, "
            "empezando por letra o número"
        )
    if not raw_path:
        raise ValueError(f"La cuenta {account_id} no tiene una ruta")
    return account_id, raw_path


def _resolve_secure_path(
    account_id: str, raw_path: str, *, owner_uid: int, expected_kind: str
) -> Path:
    if any(character in raw_path for character in ("\x00", "\n", "\r")):
        raise ValueError(f"La ruta de {account_id} contiene caracteres no permitidos")

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise ValueError(f"La ruta de {account_id} debe ser absoluta")

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"La ruta de {account_id} no existe o no es accesible") from exc

    # Comparar con el texto recibido también rechaza '.', '..', barras finales y
    # cualquier enlace simbólico, incluso si aparece en un directorio intermedio.
    if raw_path != str(resolved):
        raise ValueError(
            f"La ruta de {account_id} debe ser canónica y no contener enlaces simbólicos"
        )

    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise ValueError(f"No fue posible validar la ruta de {account_id}") from exc
    if expected_kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"La ruta de {account_id} no es un directorio real")
    if expected_kind == "file" and not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"El shared lock de {account_id} no es un archivo regular")
    kind_label = "El directorio" if expected_kind == "directory" else "El shared lock"
    if metadata.st_uid != owner_uid:
        raise ValueError(f"{kind_label} de {account_id} no pertenece al usuario actual")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError(
            f"{kind_label} de {account_id} permite acceso de grupo u otros; "
            "usa permisos privados"
        )
    return resolved


def _validate_shared_lock(
    account_id: str, raw_path: str, *, owner_uid: int
) -> tuple[Path, bool]:
    """Valida un lock existente o su padre si aún se debe crear."""

    if any(character in raw_path for character in ("\x00", "\n", "\r")):
        raise ValueError(f"La ruta de {account_id} contiene caracteres no permitidos")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise ValueError(f"La ruta de {account_id} debe ser absoluta")

    try:
        candidate.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError(f"No fue posible validar el shared lock de {account_id}") from exc
    else:
        return (
            _resolve_secure_path(
                account_id,
                raw_path,
                owner_uid=owner_uid,
                expected_kind="file",
            ),
            False,
        )

    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"El padre del shared lock de {account_id} debe existir"
        ) from exc
    canonical = resolved_parent / candidate.name
    if raw_path != str(canonical):
        raise ValueError(
            f"La ruta de {account_id} debe ser canónica y no contener enlaces simbólicos"
        )

    try:
        parent_metadata = resolved_parent.stat()
    except OSError as exc:
        raise ValueError(
            f"No fue posible validar el padre del shared lock de {account_id}"
        ) from exc
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError(
            f"El padre del shared lock de {account_id} no es un directorio real"
        )
    if parent_metadata.st_uid != owner_uid:
        raise ValueError(
            f"El padre del shared lock de {account_id} no pertenece al usuario actual"
        )
    if stat.S_IMODE(parent_metadata.st_mode) & 0o077:
        raise ValueError(
            f"El padre del shared lock de {account_id} permite acceso de grupo u otros"
        )
    return canonical, True


def _create_shared_lock(account_id: str, path: Path) -> None:
    """Crea un lock vacío y privado relativo a un padre ya validado."""

    parent_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    create_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
    )
    try:
        parent_descriptor = os.open(path.parent, parent_flags)
    except OSError as exc:
        raise ValueError(
            f"No fue posible abrir de forma segura el padre del shared lock de {account_id}"
        ) from exc

    lock_descriptor: int | None = None
    created = False
    try:
        try:
            lock_descriptor = os.open(
                path.name,
                create_flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            created = True
            os.fchmod(lock_descriptor, 0o600)
            os.fsync(lock_descriptor)
        finally:
            if lock_descriptor is not None:
                os.close(lock_descriptor)
                lock_descriptor = None
        os.fsync(parent_descriptor)
    except OSError as exc:
        if created:
            try:
                os.unlink(path.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except OSError:
                pass
        raise ValueError(
            f"No fue posible crear de forma exclusiva el shared lock de {account_id}"
        ) from exc
    finally:
        os.close(parent_descriptor)


def validate_accounts(
    values: Sequence[str],
    *,
    shared_locks: Sequence[str] = (),
    owner_uid: int | None = None,
) -> tuple[WriterAccount, ...]:
    """Valida la colección completa antes de crear los locks que falten."""

    if not values:
        raise ValueError("Se necesita al menos un --account ID=RUTA")
    if len(values) > MAX_ACCOUNTS:
        raise ValueError(f"Se permiten como máximo {MAX_ACCOUNTS} cuentas")

    selected_uid = os.getuid() if owner_uid is None else owner_uid
    account_paths: list[tuple[str, Path]] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for value in values:
        account_id, raw_path = _split_account(value)
        if account_id in seen_ids:
            raise ValueError(f"ID de cuenta duplicado: {account_id}")
        config_dir = _resolve_secure_path(
            account_id,
            raw_path,
            owner_uid=selected_uid,
            expected_kind="directory",
        )
        if config_dir in seen_paths:
            raise ValueError(f"Directorio de cuenta duplicado: {config_dir}")
        seen_ids.add(account_id)
        seen_paths.add(config_dir)
        account_paths.append((account_id, config_dir))

    locks_by_id: dict[str, Path] = {}
    locks_to_create: list[tuple[str, Path]] = []
    for value in shared_locks:
        account_id, raw_path = _split_account(value)
        if account_id not in seen_ids:
            raise ValueError(
                f"El shared lock pertenece a una cuenta no configurada: {account_id}"
            )
        if account_id in locks_by_id:
            raise ValueError(f"Shared lock duplicado para la cuenta: {account_id}")
        shared_lock, must_create = _validate_shared_lock(
            account_id, raw_path, owner_uid=selected_uid
        )
        if shared_lock in seen_paths:
            raise ValueError(f"Ruta de cuenta o shared lock duplicada: {shared_lock}")
        seen_paths.add(shared_lock)
        locks_by_id[account_id] = shared_lock
        if must_create:
            locks_to_create.append((account_id, shared_lock))

    # No se crea nada hasta haber validado la colección completa.
    for account_id, shared_lock in locks_to_create:
        _create_shared_lock(account_id, shared_lock)

    return tuple(
        WriterAccount(
            account_id=account_id,
            config_dir=config_dir,
            shared_lock=locks_by_id.get(account_id),
        )
        for account_id, config_dir in account_paths
    )


def _manifest_bytes(accounts: Sequence[WriterAccount]) -> bytes:
    serialized_accounts = []
    for account in accounts:
        serialized = {"id": account.account_id, "config_dir": str(account.config_dir)}
        if account.shared_lock is not None:
            serialized["shared_lock"] = str(account.shared_lock)
        serialized_accounts.append(serialized)
    document = {
        "version": 1,
        "claude_accounts": serialized_accounts,
    }
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def systemd_path(value: str) -> str:
    """Escapa una ruta para una directiva systemd, sin usar quoting de shell."""

    if not value or any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError("La ruta contiene caracteres no permitidos por el drop-in")

    encoded: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "%":
            encoded.append("%%")
        elif character == "\\":
            encoded.append("\\x5c")
        elif character == " ":
            encoded.append("\\x20")
        elif character in {'"', "'"} or codepoint < 0x20 or codepoint == 0x7F:
            encoded.append(f"\\x{codepoint:02x}")
        else:
            encoded.append(character)
    return "".join(encoded)


def _drop_in_bytes(accounts: Sequence[WriterAccount]) -> bytes:
    writable_paths = [
        path
        for account in accounts
        for path in (account.config_dir, account.shared_lock)
        if path is not None
    ]
    lines = [
        "# Generado por configure_writers.py; no editar a mano.",
        "[Service]",
        *[f"ReadWritePaths={systemd_path(str(path))}" for path in writable_paths],
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> bool:
    """Reemplaza ``path`` atómicamente y normaliza su modo Unix."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_symlink() and path.is_file():
        if path.read_bytes() == payload and stat.S_IMODE(path.stat().st_mode) == mode:
            return False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.fchmod(temporary.fileno(), mode)
        os.replace(temporary_name, path)
        directory_descriptor = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return True


def configure_writers(
    values: Sequence[str],
    *,
    shared_locks: Sequence[str] = (),
    manifest_path: Path | None = None,
    drop_in_path: Path | None = None,
    owner_uid: int | None = None,
) -> ConfigurationResult:
    accounts = validate_accounts(
        values, shared_locks=shared_locks, owner_uid=owner_uid
    )
    manifest = _manifest_bytes(accounts)
    drop_in = _drop_in_bytes(accounts)
    selected_manifest = MANIFEST_PATH if manifest_path is None else manifest_path
    selected_drop_in = DROP_IN_PATH if drop_in_path is None else drop_in_path
    manifest_changed = _atomic_write(selected_manifest, manifest, mode=0o600)
    drop_in_changed = _atomic_write(selected_drop_in, drop_in, mode=0o644)
    return ConfigurationResult(
        manifest_changed=manifest_changed, drop_in_changed=drop_in_changed
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account",
        action="append",
        required=True,
        metavar="ID=RUTA",
        help="cuenta Claude y su directorio privado; se puede repetir hasta 16 veces",
    )
    parser.add_argument(
        "--shared-lock",
        action="append",
        default=[],
        metavar="ID=RUTA",
        help=(
            "archivo de bloqueo privado para coordinar una cuenta compartida; "
            "el ID debe existir en --account y el archivo se crea si falta"
        ),
    )
    arguments = parser.parse_args(argv)

    try:
        result = configure_writers(
            arguments.account, shared_locks=arguments.shared_lock
        )
    except (OSError, ValueError) as exc:
        print(f"No fue posible configurar las cuentas: {exc}", file=sys.stderr)
        return 2

    print(
        f"{'Actualizado' if result.manifest_changed else 'Sin cambios'}: {MANIFEST_PATH}"
    )
    print(
        f"{'Actualizado' if result.drop_in_changed else 'Sin cambios'}: {DROP_IN_PATH}"
    )
    print("No se ejecutó systemctl. Recarga y reinicia el motor cuando corresponda.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
