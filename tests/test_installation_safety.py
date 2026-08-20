from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "scripts/instalar_servicios.py"
LINKEDIN_CONFIGURATOR_PATH = ROOT / "scripts/configure_linkedin.py"
SCANNER_PATH = ROOT / "tools/scan-secretos.sh"

SPEC = importlib.util.spec_from_file_location("instalar_servicios", INSTALLER_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

LINKEDIN_SPEC = importlib.util.spec_from_file_location(
    "configure_linkedin_for_test", LINKEDIN_CONFIGURATOR_PATH
)
assert LINKEDIN_SPEC is not None and LINKEDIN_SPEC.loader is not None
linkedin_configurator = importlib.util.module_from_spec(LINKEDIN_SPEC)
LINKEDIN_SPEC.loader.exec_module(linkedin_configurator)

from sincategorematico_bot.storage import StateStore


class TemplateInstallationTests(unittest.TestCase):
    def test_installation_forces_pause_and_simulation_before_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            state_path = Path(raw_directory) / "state.db"
            store = StateStore(state_path)
            store.set("publishing_paused", False)
            store.set("dry_run", False)
            store.close()

            installer.force_safe_startup(
                state_path=state_path, config_path=ROOT / "config.toml"
            )

            store = StateStore(state_path)
            try:
                self.assertTrue(store.get_bool("publishing_paused"))
                self.assertTrue(store.get_bool("dry_run"))
            finally:
                store.close()

    def test_linkedin_reauthorization_also_returns_to_safe_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            store = StateStore(Path(raw_directory) / "state.db")
            try:
                store.set("publishing_paused", False)
                store.set("dry_run", False)

                linkedin_configurator.force_safe_linkedin_state(store)

                self.assertTrue(store.get_bool("publishing_paused"))
                self.assertTrue(store.get_bool("dry_run"))
            finally:
                store.close()

    def test_linkedin_client_id_rejects_prose_and_urls(self) -> None:
        self.assertTrue(linkedin_configurator.plausible_client_id("86abcD9_xyz-12"))
        self.assertFalse(
            linkedin_configurator.plausible_client_id(
                "hazlo automatico usando mi perfil abierto"
            )
        )
        self.assertFalse(
            linkedin_configurator.plausible_client_id(
                "https://www.linkedin.com/developers/apps"
            )
        )

    def test_render_is_atomic_idempotent_and_canonicalizes_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            source = directory / "example.service"
            target = directory / "installed/example.service"
            source.write_text(
                "WorkingDirectory=@PROJECT_ROOT@\nEnvironmentFile=@HOME@/bot.env\n",
                encoding="utf-8",
            )
            source.chmod(0o741)

            old_target = directory / "old.service"
            old_target.write_text("plantilla vieja", encoding="utf-8")
            target.parent.mkdir()
            target.symlink_to(old_target)

            changed = installer.render_template(
                source,
                target,
                replacements={"@PROJECT_ROOT@": "/srv/bot", "@HOME@": "/home/alguien"},
            )

            self.assertTrue(changed)
            self.assertFalse(target.is_symlink())
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "WorkingDirectory=/srv/bot\nEnvironmentFile=/home/alguien/bot.env\n",
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertEqual(old_target.read_text(encoding="utf-8"), "plantilla vieja")
            self.assertFalse(
                installer.render_template(
                    source,
                    target,
                    replacements={
                        "@PROJECT_ROOT@": "/srv/bot",
                        "@HOME@": "/home/alguien",
                    },
                )
            )

    def test_render_rejects_unresolved_or_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            source = directory / "example.service"
            target = directory / "installed.service"
            source.write_text("@PROJECT_ROOT@ @HOME@", encoding="utf-8")

            with self.assertRaises(ValueError):
                installer.render_template(
                    source, target, replacements={"@PROJECT_ROOT@": "/srv/bot"}
                )
            with self.assertRaises(ValueError):
                installer.render_template(
                    source,
                    target,
                    replacements={"@PROJECT_ROOT@": "/srv\nbot", "@HOME@": "/home/user"},
                )

    def test_install_templates_accepts_a_service_only_subset(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            deploy = directory / "deploy"
            units = directory / "units"
            applications = directory / "applications"
            deploy.mkdir()
            service = "only-dashboard.service"
            (deploy / service).write_text(
                "WorkingDirectory=@PROJECT_ROOT@\n", encoding="utf-8"
            )

            with (
                mock.patch.object(installer, "DEPLOY", deploy),
                mock.patch.object(installer, "UNIT_DIR", units),
                mock.patch.object(installer, "APPLICATION_DIR", applications),
            ):
                installer.install_templates(services=(service,), desktops=())

            self.assertTrue((units / service).is_file())
            self.assertFalse(applications.exists())

    def test_real_templates_support_spaces_and_literal_percent_in_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            project = directory / "Proyecto con espacios 50%" / "bot"
            home = directory / "Usuario con espacios 25%"
            project.mkdir(parents=True)
            home.mkdir(parents=True)
            replacements = {
                "@PROJECT_ROOT@": str(project),
                "@HOME@": str(home),
            }

            rendered_services: list[Path] = []
            for name in installer.SERVICES:
                target = directory / "units" / name
                installer.render_template(
                    ROOT / "deploy" / name, target, replacements=replacements
                )
                content = target.read_text(encoding="utf-8")
                self.assertNotIn("@PROJECT_ROOT@", content)
                self.assertIn("50%%", content)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                rendered_services.append(target)

            rendered_desktops: list[Path] = []
            for name in installer.DESKTOPS:
                target = directory / "applications" / name
                installer.render_template(
                    ROOT / "deploy" / name, target, replacements=replacements
                )
                content = target.read_text(encoding="utf-8")
                self.assertIn("50%%", next(
                    line for line in content.splitlines() if line.startswith("Exec=")
                ))
                self.assertIn("Proyecto con espacios 50%", next(
                    line for line in content.splitlines() if line.startswith("Icon=")
                ))
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
                rendered_desktops.append(target)

            if shutil.which("systemd-analyze"):
                result = subprocess.run(
                    ["systemd-analyze", "--user", "verify", *map(str, rendered_services)],
                    check=False,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            if shutil.which("desktop-file-validate"):
                result = subprocess.run(
                    ["desktop-file-validate", *map(str, rendered_desktops)],
                    check=False,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


class SecretScannerTests(unittest.TestCase):
    def run_git(self, directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments], cwd=directory, check=True, text=True, capture_output=True
        )

    def run_scanner(self, directory: Path, mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCANNER_PATH), mode], cwd=directory, check=False, text=True,
            capture_output=True,
        )

    def test_staged_scan_blocks_without_echoing_the_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            self.run_git(directory, "init", "--quiet")
            safe = directory / "safe.txt"
            safe.write_text("TOKEN_NAME=SINCATEGOREMATICO_TOKEN\n", encoding="utf-8")
            self.run_git(directory, "add", "safe.txt")
            result = self.run_scanner(directory, "--staged")
            self.assertEqual(result.returncode, 0, result.stderr)

            # Se construye por partes para que esta prueba no se marque a sí misma.
            secret = "123456789:" + "Ab3_defGhijKlmNopQrStuVwXyZ012345"
            exposed = directory / "exposed.txt"
            exposed.write_text(f"telegram={secret}\n", encoding="utf-8")
            self.run_git(directory, "add", "exposed.txt")
            result = self.run_scanner(directory, "--staged")

            self.assertEqual(result.returncode, 1)
            self.assertIn("exposed.txt", result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_worktree_scan_does_not_disclose_an_assigned_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            self.run_git(directory, "init", "--quiet")
            secret = "MiClave" + "Segura_4829.ConMuchoEntropia"
            candidate = directory / "settings.txt"
            candidate.write_text(f'{{"client_secret": "{secret}"}}\n', encoding="utf-8")

            result = self.run_scanner(directory, "--todo")

            self.assertEqual(result.returncode, 1)
            self.assertIn("settings.txt", result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_worktree_scan_blocks_a_hex_only_client_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            self.run_git(directory, "init", "--quiet")
            secret = "abcdef01" * 4
            candidate = directory / "credentials.env"
            candidate.write_text(f"CLIENT_SECRET={secret}\n", encoding="utf-8")

            result = self.run_scanner(directory, "--todo")

            self.assertEqual(result.returncode, 1)
            self.assertIn("credentials.env", result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_staged_scan_blocks_a_sixteen_character_client_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            self.run_git(directory, "init", "--quiet")
            secret = "abcd" * 4
            candidate = directory / "short-secret.env"
            candidate.write_text(f"CLIENT_SECRET={secret}\n", encoding="utf-8")
            self.run_git(directory, "add", "short-secret.env")

            result = self.run_scanner(directory, "--staged")

            self.assertEqual(result.returncode, 1)
            self.assertIn("short-secret.env", result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_staged_scan_inspects_a_blob_larger_than_ten_mib(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            self.run_git(directory, "init", "--quiet")
            secret = "abcdef01" * 4
            candidate = directory / "large.bin"
            candidate.write_bytes(
                b"A" * (10 * 1024 * 1024 + 1)
                + b"\nCLIENT_SECRET="
                + secret.encode("ascii")
            )
            self.run_git(directory, "add", "large.bin")

            result = self.run_scanner(directory, "--staged")

            self.assertEqual(result.returncode, 1)
            self.assertIn("large.bin", result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_staged_scan_inspects_binary_blobs_with_nuls(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            self.run_git(directory, "init", "--quiet")
            secret = "MiSecreto" + "Binario" + "1234567890"
            candidate = directory / "payload.dat"
            candidate.write_bytes(
                b"\x00\xff\x10CLIENT_SECRET=" + secret.encode("ascii") + b"\x00"
            )
            self.run_git(directory, "add", "payload.dat")

            result = self.run_scanner(directory, "--staged")

            self.assertEqual(result.returncode, 1)
            self.assertIn("payload.dat", result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
