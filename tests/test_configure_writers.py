from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONFIGURATOR_PATH = ROOT / "scripts/configure_writers.py"
SPEC = importlib.util.spec_from_file_location(
    "configure_writers_for_test", CONFIGURATOR_PATH
)
assert SPEC is not None and SPEC.loader is not None
configurator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = configurator
SPEC.loader.exec_module(configurator)


class WriterConfiguratorTests(unittest.TestCase):
    def private_directory(self, parent: Path, name: str) -> Path:
        directory = parent / name
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        return directory

    def private_file(self, parent: Path, name: str) -> Path:
        path = parent / name
        path.write_text("contenido opaco que no debe copiarse", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_writes_exact_schema_drop_in_and_canonical_modes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            primary = self.private_directory(directory, "primary")
            backup = self.private_directory(directory, "backup")
            shared_lock = self.private_file(directory, "primary.lock")
            manifest = directory / "configuration/writers.json"
            drop_in = directory / "systemd/engine.service.d/writer-accounts.conf"

            original_read_bytes = Path.read_bytes

            def reject_credential_reads(path: Path) -> bytes:
                if path in {primary, backup, shared_lock}:
                    raise AssertionError("el configurador intentó leer una credencial")
                return original_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", reject_credential_reads):
                first = configurator.configure_writers(
                    [f"primary={primary}", f"backup={backup}"],
                    shared_locks=[f"primary={shared_lock}"],
                    manifest_path=manifest,
                    drop_in_path=drop_in,
                )

            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8")),
                {
                    "version": 1,
                    "claude_accounts": [
                        {
                            "id": "primary",
                            "config_dir": str(primary),
                            "shared_lock": str(shared_lock),
                        },
                        {"id": "backup", "config_dir": str(backup)},
                    ],
                },
            )
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(drop_in.stat().st_mode), 0o644)
            directives = [
                line
                for line in drop_in.read_text(encoding="utf-8").splitlines()
                if line.startswith("ReadWritePaths=")
            ]
            self.assertEqual(
                directives,
                [
                    f"ReadWritePaths={configurator.systemd_path(str(primary))}",
                    f"ReadWritePaths={configurator.systemd_path(str(shared_lock))}",
                    f"ReadWritePaths={configurator.systemd_path(str(backup))}",
                ],
            )
            combined_output = manifest.read_text() + drop_in.read_text()
            self.assertNotIn("contenido opaco", combined_output)
            self.assertEqual(
                first, configurator.ConfigurationResult(True, True)
            )

            second = configurator.configure_writers(
                [f"primary={primary}", f"backup={backup}"],
                shared_locks=[f"primary={shared_lock}"],
                manifest_path=manifest,
                drop_in_path=drop_in,
            )
            self.assertEqual(
                second, configurator.ConfigurationResult(False, False)
            )

            manifest.chmod(0o644)
            drop_in.chmod(0o600)
            third = configurator.configure_writers(
                [f"primary={primary}", f"backup={backup}"],
                shared_locks=[f"primary={shared_lock}"],
                manifest_path=manifest,
                drop_in_path=drop_in,
            )
            self.assertEqual(third, configurator.ConfigurationResult(True, True))
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(drop_in.stat().st_mode), 0o644)

    def test_atomic_writes_replace_symlinks_without_touching_their_targets(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            account = self.private_directory(directory, "account")
            manifest = directory / "configuration/writers.json"
            drop_in = directory / "systemd/engine.service.d/writer-accounts.conf"
            manifest.parent.mkdir(parents=True)
            drop_in.parent.mkdir(parents=True)
            old_manifest = directory / "old-manifest"
            old_drop_in = directory / "old-drop-in"
            old_manifest.write_text("anterior", encoding="utf-8")
            old_drop_in.write_text("anterior", encoding="utf-8")
            manifest.symlink_to(old_manifest)
            drop_in.symlink_to(old_drop_in)

            configurator.configure_writers(
                [f"account={account}"],
                manifest_path=manifest,
                drop_in_path=drop_in,
            )

            self.assertFalse(manifest.is_symlink())
            self.assertFalse(drop_in.is_symlink())
            self.assertEqual(old_manifest.read_text(encoding="utf-8"), "anterior")
            self.assertEqual(old_drop_in.read_text(encoding="utf-8"), "anterior")
            self.assertFalse(list(manifest.parent.glob(".writers.json.*")))
            self.assertFalse(list(drop_in.parent.glob(".writer-accounts.conf.*")))

    def test_systemd_escaping_handles_space_percent_backslash_and_quotes(self) -> None:
        value = "/srv/Writer 50%\\Claude\t\"quoted\"'"
        self.assertEqual(
            configurator.systemd_path(value),
            "/srv/Writer\\x2050%%\\x5cClaude\\x09\\x22quoted\\x22\\x27",
        )
        for unsafe in ("", "/tmp/a\x00b", "/tmp/a\nb", "/tmp/a\rb"):
            with self.subTest(unsafe=repr(unsafe)):
                with self.assertRaises(ValueError):
                    configurator.systemd_path(unsafe)

    def test_drop_in_escapes_special_names_and_contains_only_selected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            account = self.private_directory(directory, "account 50%\\cache")
            shared_lock = self.private_file(directory, "writer 25%\\lock")
            manifest = directory / "out/writers.json"
            drop_in = directory / "out/writer-accounts.conf"

            configurator.configure_writers(
                [f"special={account}"],
                shared_locks=[f"special={shared_lock}"],
                manifest_path=manifest,
                drop_in_path=drop_in,
            )

            directives = [
                line
                for line in drop_in.read_text(encoding="utf-8").splitlines()
                if line.startswith("ReadWritePaths=")
            ]
            self.assertEqual(
                directives,
                [
                    f"ReadWritePaths={str(directory)}/account\\x2050%%\\x5ccache",
                    f"ReadWritePaths={str(directory)}/writer\\x2025%%\\x5clock",
                ],
            )
            self.assertNotIn(str(manifest), "\n".join(directives))

    def test_rejects_invalid_ids_and_account_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            account = self.private_directory(directory, "account")

            invalid_values = (
                "missing-separator",
                f"={account}",
                f"Upper={account}",
                f"-leading={account}",
                f"with.dot={account}",
                f"{'a' * 65}={account}",
                "account=relative/path",
                f"account={directory / 'missing'}",
                f"account={account}/",
                f"account={directory}/./account",
                "account=/tmp/a\x00b",
                "account=/tmp/a\nb",
            )
            for value in invalid_values:
                with self.subTest(value=repr(value)):
                    with self.assertRaises(ValueError):
                        configurator.validate_accounts([value])

            with self.assertRaisesRegex(ValueError, "al menos"):
                configurator.validate_accounts([])
            with self.assertRaisesRegex(ValueError, "máximo"):
                configurator.validate_accounts(
                    [f"account{number}=/not-inspected" for number in range(17)]
                )

    def test_rejects_non_directory_symlink_owner_permissions_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            first = self.private_directory(directory, "first")
            second = self.private_directory(directory, "second")
            regular_file = self.private_file(directory, "regular-file")
            symlink = directory / "account-link"
            symlink.symlink_to(first, target_is_directory=True)
            public = self.private_directory(directory, "public")
            public.chmod(0o750)

            invalid_collections = (
                [f"first={regular_file}"],
                [f"first={symlink}"],
                [f"first={public}"],
                [f"same={first}", f"same={second}"],
                [f"first={first}", f"second={first}"],
            )
            for values in invalid_collections:
                with self.subTest(values=values):
                    with self.assertRaises(ValueError):
                        configurator.validate_accounts(values)

            with self.assertRaisesRegex(ValueError, "usuario actual"):
                configurator.validate_accounts(
                    [f"first={first}"], owner_uid=os.getuid() + 1
                )

            real_parent = self.private_directory(directory, "real-parent")
            nested = self.private_directory(real_parent, "nested")
            parent_link = directory / "parent-link"
            parent_link.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "enlaces simbólicos"):
                configurator.validate_accounts(
                    [f"nested={parent_link / nested.name}"]
                )

    def test_validates_optional_shared_locks_without_reading_them(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            first = self.private_directory(directory, "first")
            second = self.private_directory(directory, "second")
            lock = self.private_file(directory, "writer.lock")
            other_lock = self.private_file(directory, "other.lock")

            invalid_locks = (
                ["unknown=" + str(lock)],
                ["first=" + str(first)],
                ["first=" + str(lock), "first=" + str(other_lock)],
                ["first=relative/writer.lock"],
                ["first=/tmp/a\x00b"],
                ["first=/tmp/a\nb"],
            )
            for locks in invalid_locks:
                with self.subTest(locks=locks):
                    with self.assertRaises(ValueError):
                        configurator.validate_accounts(
                            [f"first={first}"], shared_locks=locks
                        )

            with self.assertRaisesRegex(ValueError, "duplicada"):
                configurator.validate_accounts(
                    [f"first={first}", f"second={second}"],
                    shared_locks=[f"first={lock}", f"second={lock}"],
                )

            lock.chmod(0o640)
            with self.assertRaisesRegex(ValueError, "grupo u otros"):
                configurator.validate_accounts(
                    [f"first={first}"], shared_locks=[f"first={lock}"]
                )
            lock.chmod(0o600)
            lock_link = directory / "lock-link"
            lock_link.symlink_to(lock)
            with self.assertRaisesRegex(ValueError, "enlaces simbólicos"):
                configurator.validate_accounts(
                    [f"first={first}"], shared_locks=[f"first={lock_link}"]
                )
            real_stat = Path.stat

            def stat_with_foreign_lock(path: Path, *args: object, **kwargs: object):
                metadata = real_stat(path, *args, **kwargs)
                if path == lock:
                    fields = list(metadata)
                    fields[4] = os.getuid() + 1
                    return os.stat_result(fields)
                return metadata

            with mock.patch.object(Path, "stat", stat_with_foreign_lock):
                with self.assertRaisesRegex(ValueError, "usuario actual"):
                    configurator.validate_accounts(
                        [f"first={first}"], shared_locks=[f"first={lock}"]
                    )

    def test_precreates_a_missing_shared_lock_privately_and_exclusively(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            account = self.private_directory(directory, "account")
            lock_parent = self.private_directory(directory, "locks")
            shared_lock = lock_parent / "writer.lock"
            manifest = directory / "out/writers.json"
            drop_in = directory / "out/writer-accounts.conf"

            original_open = os.open
            original_fsync = os.fsync
            fsynced_objects: set[tuple[int, int]] = set()

            def track_fsync(descriptor: int) -> None:
                metadata = os.fstat(descriptor)
                fsynced_objects.add((metadata.st_dev, metadata.st_ino))
                original_fsync(descriptor)

            with (
                mock.patch.object(
                    configurator.os, "open", wraps=original_open
                ) as opened,
                mock.patch.object(configurator.os, "fsync", side_effect=track_fsync),
            ):
                configurator.configure_writers(
                    [f"account={account}"],
                    shared_locks=[f"account={shared_lock}"],
                    manifest_path=manifest,
                    drop_in_path=drop_in,
                )

            self.assertTrue(shared_lock.is_file())
            self.assertEqual(shared_lock.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(shared_lock.stat().st_mode), 0o600)
            parent_metadata = lock_parent.stat()
            self.assertIn(
                (parent_metadata.st_dev, parent_metadata.st_ino), fsynced_objects
            )
            create_calls = [
                call
                for call in opened.call_args_list
                if call.args
                and call.args[0] == shared_lock.name
                and call.kwargs.get("dir_fd") is not None
            ]
            self.assertEqual(len(create_calls), 1)
            create_flags = create_calls[0].args[1]
            for required_flag in (
                os.O_CREAT,
                os.O_EXCL,
                os.O_CLOEXEC,
                os.O_NOFOLLOW,
            ):
                self.assertEqual(create_flags & required_flag, required_flag)
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8"))[
                    "claude_accounts"
                ][0]["shared_lock"],
                str(shared_lock),
            )
            self.assertIn(
                f"ReadWritePaths={configurator.systemd_path(str(shared_lock))}",
                drop_in.read_text(encoding="utf-8").splitlines(),
            )

    def test_missing_shared_lock_requires_a_real_private_owned_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            account = self.private_directory(directory, "account")
            missing_parent = directory / "missing"
            public_parent = self.private_directory(directory, "public-locks")
            public_parent.chmod(0o750)
            real_parent = self.private_directory(directory, "real-locks")
            parent_link = directory / "linked-locks"
            parent_link.symlink_to(real_parent, target_is_directory=True)

            invalid_paths = (
                missing_parent / "writer.lock",
                public_parent / "writer.lock",
                parent_link / "writer.lock",
            )
            for shared_lock in invalid_paths:
                with self.subTest(shared_lock=shared_lock):
                    with self.assertRaises(ValueError):
                        configurator.validate_accounts(
                            [f"account={account}"],
                            shared_locks=[f"account={shared_lock}"],
                        )
                    self.assertFalse(shared_lock.exists())
            self.assertFalse(missing_parent.exists())

            real_stat = Path.stat

            def stat_with_foreign_parent(
                path: Path, *args: object, **kwargs: object
            ):
                metadata = real_stat(path, *args, **kwargs)
                if path == real_parent:
                    fields = list(metadata)
                    fields[4] = os.getuid() + 1
                    return os.stat_result(fields)
                return metadata

            foreign_lock = real_parent / "foreign.lock"
            with mock.patch.object(Path, "stat", stat_with_foreign_parent):
                with self.assertRaisesRegex(ValueError, "usuario actual"):
                    configurator.validate_accounts(
                        [f"account={account}"],
                        shared_locks=[f"account={foreign_lock}"],
                    )
            self.assertFalse(foreign_lock.exists())

            planned_lock = real_parent / "planned.lock"
            with self.assertRaises(ValueError):
                configurator.validate_accounts(
                    [f"account={account}"],
                    shared_locks=[
                        f"account={planned_lock}",
                        f"unknown={real_parent / 'unknown.lock'}",
                    ],
                )
            self.assertFalse(planned_lock.exists())

    def test_shared_lock_creation_fails_closed_if_a_symlink_appears(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            account = self.private_directory(directory, "account")
            lock_parent = self.private_directory(directory, "locks")
            shared_lock = lock_parent / "writer.lock"
            victim = self.private_file(directory, "victim")
            manifest = directory / "out/writers.json"
            drop_in = directory / "out/writer-accounts.conf"
            original_open = os.open
            raced = False

            def race_before_create(
                path: str | bytes | int | os.PathLike[str],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal raced
                if (
                    path == shared_lock.name
                    and dir_fd is not None
                    and flags & os.O_CREAT
                    and not raced
                ):
                    os.symlink(victim, shared_lock.name, dir_fd=dir_fd)
                    raced = True
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(
                configurator.os, "open", side_effect=race_before_create
            ):
                with self.assertRaisesRegex(ValueError, "forma exclusiva"):
                    configurator.configure_writers(
                        [f"account={account}"],
                        shared_locks=[f"account={shared_lock}"],
                        manifest_path=manifest,
                        drop_in_path=drop_in,
                    )

            self.assertTrue(shared_lock.is_symlink())
            self.assertEqual(
                victim.read_text(encoding="utf-8"),
                "contenido opaco que no debe copiarse",
            )
            self.assertFalse(manifest.exists())
            self.assertFalse(drop_in.exists())

    def test_failed_lock_initialization_removes_the_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            account = self.private_directory(directory, "account")
            lock_parent = self.private_directory(directory, "locks")
            shared_lock = lock_parent / "writer.lock"
            manifest = directory / "out/writers.json"
            drop_in = directory / "out/writer-accounts.conf"
            original_fsync = os.fsync

            def fail_regular_file_fsync(descriptor: int) -> None:
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("fallo simulado")
                original_fsync(descriptor)

            with mock.patch.object(
                configurator.os, "fsync", side_effect=fail_regular_file_fsync
            ):
                with self.assertRaisesRegex(ValueError, "forma exclusiva"):
                    configurator.configure_writers(
                        [f"account={account}"],
                        shared_locks=[f"account={shared_lock}"],
                        manifest_path=manifest,
                        drop_in_path=drop_in,
                    )

            self.assertFalse(shared_lock.exists())
            self.assertFalse(manifest.exists())
            self.assertFalse(drop_in.exists())

    def test_cli_does_not_call_systemctl_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            account = self.private_directory(directory, "account")
            manifest = directory / "configuration/writers.json"
            drop_in = directory / "systemd/engine.service.d/writer-accounts.conf"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(configurator, "MANIFEST_PATH", manifest),
                mock.patch.object(configurator, "DROP_IN_PATH", drop_in),
                mock.patch.object(subprocess, "run") as run,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = configurator.main(["--account", f"account={account}"])

            self.assertEqual(status, 0, stderr.getvalue())
            self.assertTrue(manifest.is_file())
            self.assertTrue(drop_in.is_file())
            run.assert_not_called()
            self.assertIn("No se ejecutó systemctl", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
