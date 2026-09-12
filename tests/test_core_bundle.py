from __future__ import annotations

from hashlib import sha256
import io
import json
from pathlib import Path
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from qingtian_core.bundle import build_bundle, scan_tree, verify_bundle


class BundleTest(unittest.TestCase):
    @staticmethod
    def write_allowlist(root: Path, paths: list[str]) -> None:
        (root / "release-allowlist.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "paths": ["release-allowlist.json", *paths],
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def write_self_consistent_archive(
        archive_path: Path,
        files: dict[str, bytes],
        *,
        allowlist: object | None = None,
    ) -> None:
        root = "portable-kit"
        declared = ["release-allowlist.json", *files]
        allowlist_document = (
            {"schema_version": 1, "paths": declared}
            if allowlist is None
            else allowlist
        )
        payloads = {
            **files,
            "release-allowlist.json": json.dumps(
                allowlist_document, sort_keys=True
            ).encode("utf-8"),
        }
        manifest = "".join(
            f"{sha256(payload).hexdigest()}  {name}\n"
            for name, payload in sorted(payloads.items())
        ).encode("utf-8")
        with tarfile.open(archive_path, "w:gz") as archive:
            root_info = tarfile.TarInfo(root)
            root_info.type = tarfile.DIRTYPE
            root_info.mode = 0o755
            archive.addfile(root_info)
            for name, payload in sorted(payloads.items()):
                info = tarfile.TarInfo(f"{root}/{name}")
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
            manifest_info = tarfile.TarInfo(f"{root}/MANIFEST.sha256")
            manifest_info.size = len(manifest)
            manifest_info.mode = 0o644
            archive.addfile(manifest_info, io.BytesIO(manifest))

    def test_build_and_verify_manifest_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            source = base / "portable-kit"
            source.mkdir()
            (source / "README.md").write_text("portable\n", encoding="utf-8")
            (source / "docs").mkdir()
            (source / "docs" / "example.json").write_text("{}\n", encoding="utf-8")
            (source / "not-allowlisted.txt").write_text("excluded\n", encoding="utf-8")
            self.write_allowlist(source, ["README.md", "docs/example.json"])
            (source / "release").mkdir()
            (source / "release" / "old.tgz").write_bytes(b"excluded")
            archive = base / "kit.tgz"
            built = build_bundle(source, archive)
            verified = verify_bundle(archive)
            self.assertEqual(built["files"], 3)
            self.assertEqual(verified["files"], 3)
            with tarfile.open(archive, "r:gz") as handle:
                self.assertEqual(
                    {Path(item.name).parts[0] for item in handle.getmembers()},
                    {"qingtianAI"},
                )
            second_archive = base / "kit-second.tgz"
            second = build_bundle(source, second_archive)
            self.assertEqual(built["sha256"], second["sha256"])

    def test_current_root_package_layout_is_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            source = base / "qingtian-ai"
            (source / "qingtian_core").mkdir(parents=True)
            (source / "qingtian_kb").mkdir()
            (source / "contracts").mkdir()
            paths = [
                "qingtian_core/__init__.py",
                "qingtian_kb/__init__.py",
                "contracts/provider-request.schema.json",
            ]
            for relative in paths:
                (source / relative).write_text("{}\n", encoding="utf-8")
            self.write_allowlist(source, paths)
            archive = base / "kit.tgz"
            built = build_bundle(source, archive)
            self.assertEqual(built["files"], 4)
            self.assertEqual(verify_bundle(archive)["files"], 4)

    def test_secret_scan_rejects_private_key_material(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            root = Path(temp_name)
            (root / "docs").mkdir()
            (root / "docs" / "secret.txt").write_text(
                "-----BEGIN " + "PRIVATE KEY-----\nnot-real\n", encoding="utf-8"
            )
            self.write_allowlist(root, ["docs/secret.txt"])
            self.assertEqual(scan_tree(root)[0]["kind"], "private-key")
            self.assertNotIn("match", scan_tree(root)[0])

    def test_unsafe_archive_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            archive = Path(temp_name) / "unsafe.tgz"
            payload = Path(temp_name) / "payload"
            payload.write_text("x", encoding="utf-8")
            with tarfile.open(archive, "w:gz") as handle:
                handle.add(payload, arcname="../payload")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                verify_bundle(archive)

    def test_verifier_rejects_files_outside_static_release_policy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            forbidden = (
                "secrets.env",
                "vault/private.md",
                ".state/index.db",
                ".qingtian-knowledge-root",
                "unknown/data.txt",
                "config/sources.json",
                "docs/.env",
                "docs/.env.production",
                "docs/state.db",
                "docs/state.sqlite",
                "docs/state.sqlite3",
                "docs/private.key",
                "docs/certificate.pem",
                "docs/identity.p12",
                "docs/identity.pfx",
                "docs/build/generated.txt",
                "qingtian_core/__pycache__/module.pyc",
                "qingtian_core/package.egg-info/PKG-INFO",
                "docs/vault/private.md",
                "docs/.state/index.json",
                "docs/source-archive/input.md",
                "docs/receipts/result.json",
                "docs/checkpoints/state.json",
                "docs/runtime-data/cache.json",
                "docs/venv/module.py",
                "docs/sources.json",
                "docs/.qingtian-knowledge-root",
                "docs/.env.example/secret.txt",
            )
            for index, relative in enumerate(forbidden):
                with self.subTest(relative=relative):
                    archive = base / f"forbidden-{index}.tgz"
                    self.write_self_consistent_archive(
                        archive, {relative: b"synthetic\n"}
                    )
                    with self.assertRaisesRegex(ValueError, "static release policy"):
                        verify_bundle(archive)

    def test_builder_and_verifier_share_sensitive_path_policy(self) -> None:
        forbidden = (
            "config/sources.json",
            "docs/.env.local",
            "docs/runtime.db",
            "docs/private.pem",
            "docs/dist/generated.txt",
            "docs/vault/private.md",
            "docs/sources.json",
            "docs/.qingtian-knowledge-root",
            "docs/.env.example/secret.txt",
        )
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            for index, relative in enumerate(forbidden):
                with self.subTest(relative=relative):
                    source = base / f"source-{index}"
                    target = source / relative
                    target.parent.mkdir(parents=True)
                    target.write_text("synthetic\n", encoding="utf-8")
                    self.write_allowlist(source, [relative])
                    with self.assertRaisesRegex(ValueError, "unsafe release allowlist"):
                        build_bundle(source, base / f"rejected-{index}.tgz")

    def test_env_example_is_the_only_env_file_allowed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            source = base / "portable-kit"
            (source / "docs").mkdir(parents=True)
            (source / ".env.example").write_text(
                "EXAMPLE_VALUE=replace-me\n", encoding="utf-8"
            )
            (source / "docs" / ".env.example").write_text(
                "EXAMPLE_VALUE=replace-me\n", encoding="utf-8"
            )
            self.write_allowlist(
                source, [".env.example", "docs/.env.example"]
            )
            archive = base / "env-example.tgz"
            build_bundle(source, archive)
            self.assertEqual(verify_bundle(archive)["files"], 3)

    def test_verifier_rejects_malformed_archived_allowlist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            path = "README.md"
            malformed = (
                [],
                {"schema_version": True, "paths": ["release-allowlist.json", path]},
                {
                    "schema_version": 1,
                    "paths": ["release-allowlist.json", path],
                    "extra": True,
                },
                {
                    "schema_version": 1,
                    "paths": ["release-allowlist.json", path, path],
                },
                {
                    "schema_version": 1,
                    "paths": ["release-allowlist.json", "docs//example.md"],
                },
            )
            for index, allowlist in enumerate(malformed):
                with self.subTest(allowlist=allowlist):
                    archive = base / f"malformed-{index}.tgz"
                    self.write_self_consistent_archive(
                        archive,
                        {path: b"portable\n"},
                        allowlist=allowlist,
                    )
                    with self.assertRaises(ValueError):
                        verify_bundle(archive)

    @unittest.skipUnless(os.name == "posix", "symlink escape setup requires POSIX")
    def test_allowlist_rejects_a_parent_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            root = base / "portable-kit"
            outside = base / "outside"
            (root / "docs").mkdir(parents=True)
            outside.mkdir()
            (outside / "private.txt").write_text("outside\n", encoding="utf-8")
            (root / "docs" / "linked").symlink_to(outside, target_is_directory=True)
            self.write_allowlist(root, ["docs/linked/private.txt"])
            with self.assertRaisesRegex(ValueError, "missing, unsafe, or outside"):
                build_bundle(root, base / "unsafe.tgz")

    def test_portable_entrypoints_remain_executable_after_extract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            source = base / "portable-kit"
            source.mkdir()
            entrypoint = source / "qingtian"
            entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            # Reproduce staging from a checkout that does not preserve executable bits.
            entrypoint.chmod(0o644)
            self.write_allowlist(source, ["qingtian"])
            archive_path = base / "kit.tgz"
            build_bundle(source, archive_path)
            verify_bundle(archive_path)
            with tarfile.open(archive_path, "r:gz") as archive:
                member = archive.getmember("qingtianAI/qingtian")
                self.assertEqual(member.mode & 0o777, 0o755)
            if os.name != "posix":
                return
            extracted = base / "extracted"
            extracted.mkdir()
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(extracted, filter="data")
            restored = extracted / "qingtianAI" / "qingtian"
            self.assertTrue(os.access(restored, os.X_OK))
            completed = subprocess.run([str(restored)], check=False)
            self.assertEqual(completed.returncode, 0)

    def test_archive_resource_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            base = Path(temp_name)
            source = base / "portable-kit"
            source.mkdir()
            (source / "README.md").write_text("more than one byte\n", encoding="utf-8")
            self.write_allowlist(source, ["README.md"])
            archive_path = base / "kit.tgz"
            build_bundle(source, archive_path)
            with patch("qingtian_core.bundle.MAX_ARCHIVE_TOTAL_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "total uncompressed"):
                    verify_bundle(archive_path)

    def test_bundle_output_cannot_overwrite_an_allowlisted_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qingtian-bundle-test-") as temp_name:
            source = Path(temp_name) / "portable-kit"
            source.mkdir()
            readme = source / "README.md"
            readme.write_text("preserve me\n", encoding="utf-8")
            self.write_allowlist(source, ["README.md"])
            with self.assertRaisesRegex(ValueError, "release/"):
                build_bundle(source, readme)
            self.assertEqual(readme.read_text(encoding="utf-8"), "preserve me\n")


if __name__ == "__main__":
    unittest.main()
