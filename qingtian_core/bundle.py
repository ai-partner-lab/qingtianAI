from __future__ import annotations

from hashlib import sha256
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
from typing import Iterable


EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".nox",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".qingtian",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    "node_modules",
    "release",
}
EXCLUDED_NAMES = {".DS_Store", "MANIFEST.sha256"}
FORBIDDEN_EXACT_PATHS = {"config/sources.json"}
FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
                      ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm",
                      ".key", ".pem", ".p12", ".pfx")
FORBIDDEN_DATA_PARTS = {
    ".data",
    ".state",
    "checkpoints",
    "receipts",
    "runtime-data",
    "source-archive",
    "vault",
    "venv",
}
ALLOWLIST_NAME = "release-allowlist.json"
EXECUTABLE_PATHS = {"qingtian", "qingtian-kb", "scripts/bootstrap.sh", "scripts/smoke.py"}
MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
ALLOWED_ROOT_FILES = {
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "MANIFEST.in",
    "Makefile",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "VERSION",
    "pyproject.toml",
    "qingtian",
    "qingtian-kb",
    ALLOWLIST_NAME,
}
ALLOWED_ROOT_DIRECTORIES = {
    ".github",
    "config",
    "contracts",
    "docs",
    "examples",
    "qingtian_core",
    "qingtian_engine",
    "qingtian_kb",
    "schemas",
    "scripts",
    "tests",
}
SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{24,}\b"),
    "openai-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "absolute-home": re.compile(
        r"(?:/"
        + r"Users/[^/\s]+|/"
        + r"home/[^/\s]+|[A-Za-z]:\\"
        + r"Users\\[^\\\s]+)"
    ),
}


def _forbidden_relative(relative: PurePosixPath, *, is_dir: bool) -> bool:
    if relative.as_posix() in FORBIDDEN_EXACT_PATHS:
        return True
    for index, part in enumerate(relative.parts):
        lowered = part.lower()
        is_final = index == len(relative.parts) - 1
        if lowered in FORBIDDEN_DATA_PARTS:
            return True
        if lowered in {".qingtian-knowledge-root", "sources.json"}:
            return True
        if lowered == ".env" or lowered.startswith(".env."):
            if lowered == ".env.example" and is_final and not is_dir:
                continue
            return True
        if lowered.endswith(FORBIDDEN_SUFFIXES) or lowered.endswith((".egg-info", ".local.json", ".pid")):
            return True
    return False


def _allowed_relative(relative: PurePosixPath, *, is_dir: bool) -> bool:
    if not relative.parts or relative.as_posix() in {"", "."}:
        return False
    top_level = relative.parts[0]
    in_allowlist = (
        top_level in (ALLOWED_ROOT_DIRECTORIES if is_dir else ALLOWED_ROOT_FILES)
        if len(relative.parts) == 1
        else top_level in ALLOWED_ROOT_DIRECTORIES
    )
    return (
        in_allowlist
        and not any(part.lower() in EXCLUDED_PARTS for part in relative.parts)
        and relative.name not in EXCLUDED_NAMES
        and not _forbidden_relative(relative, is_dir=is_dir)
    )


def _allowed(path: Path, root: Path) -> bool:
    relative = PurePosixPath(path.relative_to(root).as_posix())
    return _allowed_relative(relative, is_dir=path.is_dir())


def _validate_allowlist_document(document: object) -> list[str]:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "paths"}
        or not isinstance(document.get("schema_version"), int)
        or isinstance(document.get("schema_version"), bool)
        or document.get("schema_version") != 1
        or not isinstance(document.get("paths"), list)
    ):
        raise ValueError(f"unsupported {ALLOWLIST_NAME} contract")
    declared = document["paths"]
    if (
        not declared
        or any(not isinstance(value, str) or not value for value in declared)
        or len(declared) != len(set(declared))
        or ALLOWLIST_NAME not in declared
    ):
        raise ValueError(f"{ALLOWLIST_NAME} paths must be non-empty, unique, and include itself")
    for value in declared:
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "." in relative.parts
            or relative.as_posix() != value
            or not _allowed_relative(relative, is_dir=False)
        ):
            raise ValueError(f"unsafe release allowlist path: {value}")
    return declared


def _release_allowlist(root: Path) -> list[Path]:
    allowlist_path = root / ALLOWLIST_NAME
    if not allowlist_path.is_file() or allowlist_path.is_symlink():
        raise ValueError(f"release root requires a regular {ALLOWLIST_NAME}")
    try:
        document = json.loads(allowlist_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {ALLOWLIST_NAME}: {exc}") from exc
    declared = _validate_allowlist_document(document)
    paths: list[Path] = []
    for value in sorted(declared):
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
            raise ValueError(f"unsafe release allowlist path: {value}")
        candidate = root.joinpath(*relative.parts)
        cursor = root
        has_symlink_component = False
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                has_symlink_component = True
                break
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, ValueError):
            resolved = candidate
            has_symlink_component = True
        if (
            has_symlink_component
            or not resolved.is_file()
            or not _allowed(candidate, root)
        ):
            raise ValueError(f"release allowlist entry is missing, unsafe, or outside top-level policy: {value}")
        paths.append(resolved)
    return paths


def iter_release_files(root: Path) -> Iterable[Path]:
    yield from _release_allowlist(root)


def scan_tree(root: str | Path) -> list[dict[str, str]]:
    root_path = Path(root).resolve()
    findings: list[dict[str, str]] = []
    for path in iter_release_files(root_path):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            match = pattern.search(text)
            if match:
                findings.append(
                    {
                        "kind": label,
                        "path": path.relative_to(root_path).as_posix(),
                        "match_sha256": sha256(match.group(0).encode("utf-8")).hexdigest(),
                    }
                )
    return findings


def _digest(path: Path) -> str:
    checksum = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def _copy_release_file(source: Path, target: Path, root: Path) -> None:
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"release entry is not a regular file: {source.relative_to(root)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader:
        opened = os.fstat(reader.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"release entry changed while opening: {source.relative_to(root)}")
        with target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
        after = os.fstat(reader.fileno())
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ValueError(f"release entry changed while copying: {source.relative_to(root)}")
    try:
        source.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"release entry escaped its root while copying: {source.name}") from exc


def build_bundle(root: str | Path, output: str | Path) -> dict[str, object]:
    root_path = Path(root).resolve()
    output_path = Path(output).resolve()
    if not root_path.is_dir():
        raise ValueError(f"release root is not a directory: {root_path}")
    try:
        output_relative = output_path.relative_to(root_path)
    except ValueError:
        output_relative = None
    if output_relative is not None and (
        not output_relative.parts or output_relative.parts[0] != "release"
    ):
        raise ValueError("an output inside the release root must stay under release/")
    findings = scan_tree(root_path)
    if findings:
        raise ValueError(f"release scan failed: {json.dumps(findings, ensure_ascii=False)}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qingtian-release-") as temp_name:
        staging = Path(temp_name) / root_path.name
        staging.mkdir()
        manifest_lines = []
        copied = 0
        for source in iter_release_files(root_path):
            relative = source.relative_to(root_path)
            target = staging / relative
            _copy_release_file(source, target, root_path)
            manifest_lines.append(f"{_digest(target)}  {relative.as_posix()}")
            copied += 1
        (staging / "MANIFEST.sha256").write_text(
            "\n".join(manifest_lines) + "\n", encoding="utf-8"
        )
        staged_findings = scan_tree(staging)
        if staged_findings:
            raise ValueError(
                f"staged release scan failed: {json.dumps(staged_findings, ensure_ascii=False)}"
            )
        with output_path.open("wb") as raw_output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for source in [staging, *sorted(staging.rglob("*"))]:
                        relative = source.relative_to(staging.parent).as_posix()
                        info = archive.gettarinfo(str(source), arcname=relative)
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        if source.is_dir():
                            info.mode = 0o755
                        else:
                            staged_relative = source.relative_to(staging).as_posix()
                            info.mode = 0o755 if staged_relative in EXECUTABLE_PATHS else 0o644
                        if source.is_file():
                            with source.open("rb") as handle:
                                archive.addfile(info, handle)
                        else:
                            archive.addfile(info)
    return {"output": output_path.name, "files": copied, "sha256": _digest(output_path)}


def _safe_member(member: tarfile.TarInfo) -> bool:
    path = PurePosixPath(member.name)
    return (
        bool(path.parts)
        and member.name.rstrip("/") == path.as_posix()
        and not path.is_absolute()
        and ".." not in path.parts
        and not member.issym()
        and not member.islnk()
        and (member.isfile() or member.isdir())
    )


def verify_bundle(bundle: str | Path) -> dict[str, object]:
    bundle_path = Path(bundle).resolve()
    if bundle_path.stat().st_size > MAX_ARCHIVE_COMPRESSED_BYTES:
        raise ValueError("archive exceeds the compressed size limit")
    with tarfile.open(bundle_path, "r:gz") as archive:
        members: list[tarfile.TarInfo] = []
        while member := archive.next():
            members.append(member)
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive exceeds the member count limit")
        if not members or any(not _safe_member(member) for member in members):
            raise ValueError("archive contains an unsafe or unsupported member")
        file_members = [member for member in members if member.isfile()]
        if any(member.size < 0 or member.size > MAX_ARCHIVE_FILE_BYTES for member in file_members):
            raise ValueError("archive exceeds the per-file size limit")
        if sum(member.size for member in file_members) > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("archive exceeds the total uncompressed size limit")
        member_names = [member.name for member in members]
        if len(member_names) != len(set(member_names)):
            raise ValueError("archive contains duplicate member names")
        manifests = [member for member in members if member.name.endswith("/MANIFEST.sha256")]
        if len(manifests) != 1:
            raise ValueError("archive must contain exactly one MANIFEST.sha256")
        manifest_parts = PurePosixPath(manifests[0].name).parts
        if len(manifest_parts) != 2 or manifest_parts[1] != "MANIFEST.sha256":
            raise ValueError("archive manifest must be at the release root")
        if manifests[0].size > MAX_MANIFEST_BYTES:
            raise ValueError("archive manifest exceeds the size limit")
        manifest_handle = archive.extractfile(manifests[0])
        if manifest_handle is None:
            raise ValueError("manifest is not readable")
        expected: dict[str, str] = {}
        for line in io.TextIOWrapper(manifest_handle, encoding="utf-8"):
            digest, separator, relative = line.rstrip("\n").partition("  ")
            if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("malformed manifest line")
            relative_path = PurePosixPath(relative)
            if (
                not relative
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or relative == "MANIFEST.sha256"
                or relative in expected
            ):
                raise ValueError("unsafe or duplicate manifest path")
            expected[relative] = digest
        root_name = manifest_parts[0]
        root_members = [
            member
            for member in members
            if member.name.rstrip("/") == root_name and member.isdir()
        ]
        if len(root_members) != 1:
            raise ValueError("archive must contain exactly one release root directory")
        actual: dict[str, str] = {}
        secret_findings: list[dict[str, str]] = []
        archived_allowlist: dict[str, object] | None = None
        for member in members:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != root_name:
                raise ValueError("archive contains a member outside its release root")
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError("archive release root must be a directory")
                relative = "."
            else:
                relative_path = PurePosixPath(*parts[1:])
                relative = relative_path.as_posix()
                if member.name != manifests[0].name and not _allowed_relative(
                    relative_path, is_dir=member.isdir()
                ):
                    raise ValueError(
                        f"archive member violates the static release policy: {member.name}"
                    )
            expected_mode = (
                0o755
                if member.isdir() or relative in EXECUTABLE_PATHS
                else 0o644
            )
            if stat.S_IMODE(member.mode) != expected_mode:
                raise ValueError(
                    f"archive member has unexpected mode: {member.name} "
                    f"({stat.S_IMODE(member.mode):04o}, expected {expected_mode:04o})"
                )
            if not member.isfile() or member.name == manifests[0].name:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"archive member is not readable: {member.name}")
            payload = handle.read()
            actual[relative] = sha256(payload).hexdigest()
            try:
                decoded = payload.decode("utf-8")
            except UnicodeDecodeError:
                decoded = ""
            if relative == ALLOWLIST_NAME:
                try:
                    archived_allowlist = json.loads(decoded)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"archive contains invalid {ALLOWLIST_NAME}") from exc
            for label, pattern in SECRET_PATTERNS.items():
                match = pattern.search(decoded)
                if match:
                    secret_findings.append(
                        {
                            "kind": label,
                            "path": relative,
                            "match_sha256": sha256(match.group(0).encode("utf-8")).hexdigest(),
                        }
                    )
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            changed = sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key])
            raise ValueError(f"manifest mismatch: missing={missing}, extra={extra}, changed={changed}")
        if archived_allowlist is None:
            raise ValueError(f"archive is missing a supported {ALLOWLIST_NAME}")
        declared_paths = _validate_allowlist_document(archived_allowlist)
        if set(declared_paths) != set(actual):
            raise ValueError("archive contents do not exactly match the release allowlist")
        if secret_findings:
            raise ValueError(
                f"archive release scan failed: {json.dumps(secret_findings, ensure_ascii=False)}"
            )
    return {"bundle": bundle_path.name, "files": len(actual), "sha256": _digest(bundle_path)}
