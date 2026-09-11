"""Source/bundle distribution contracts; no recorder, model, or service is run."""
from __future__ import annotations

import ast
import gzip
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from qingtian_core.bundle import build_bundle, verify_bundle


ROOT = Path(__file__).resolve().parents[1]
SHOWCASE_FILES = {
    'README.md', 'scenario.json', 'engine_bridge.py', 'record.cjs',
    'verify_recording.py', 'render.cjs', 'qa_media.cjs', 'check_playback.cjs',
}
SHOWCASE_ASSETS = {
    'docs/assets/showcase/actual-board-public.png',
    'docs/assets/showcase/poster.png',
    'docs/assets/showcase/qingtian-showcase-short.mp4',
}
APPROVED_VIDEO_SHA256 = 'ce7885bcbfc6de9ba0e06dc4802d7f81a04385fe16e2dfbfd5335218d3ff3faf'


def missing_relative_links(root: Path) -> list[str]:
    """Validate local Markdown file targets, not remote URLs or heading slugs."""
    missing = []
    declared = json.loads((root / 'release-allowlist.json').read_text())['paths']
    for page in sorted(root / name for name in declared if name.endswith('.md')):
        text = re.sub(r'```.*?```', '', page.read_text(encoding='utf-8'), flags=re.S)
        for raw in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)', text):
            target = urlsplit(raw.strip('<>'))
            if target.scheme or target.netloc or not target.path:
                continue
            path = (page.parent / unquote(target.path)).resolve()
            if not path.is_relative_to(root.resolve()) or not path.exists():
                missing.append(f'{page.relative_to(root)} -> {raw}')
    return missing


def archive_files(archive: Path) -> dict[str, bytes]:
    with tarfile.open(archive, 'r:gz') as handle:
        return {
            str(PurePosixPath(*PurePosixPath(member.name).parts[1:])):
                handle.extractfile(member).read()
            for member in handle if member.isfile()
        }


def assert_public_archive_headers(path: Path) -> dict[str, int]:
    """Audit every real archive member, not just source file payloads."""
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            if archive.comment:
                raise ValueError('wheel archive comment is not empty')
            members = archive.infolist()
            for member in members:
                if member.extra or member.comment or member.flag_bits & 1 or (member.external_attr >> 16) & 0o7000:
                    raise ValueError('wheel contains extra/comment/encryption/special-mode metadata')
        return {'members_checked': len(members)}
    with path.open('rb') as handle:
        header = handle.read(10)
    if header[:4] != b'\x1f\x8b\x08\x00' or header[4:8] != b'\x00' * 4:
        raise ValueError('gzip must have no optional filename/comment/extra/header and zero timestamp')
    with tarfile.open(path, 'r:gz') as archive:
        if archive.pax_headers:
            raise ValueError('global PAX metadata is not permitted')
        members = archive.getmembers()
        for member in members:
            if (member.uid != 0 or member.gid != 0 or member.uname or member.gname
                    or member.mtime != 0 or member.mode & 0o7000
                    or not (member.isfile() or member.isdir())
                    or set(member.pax_headers) - {'path'}):
                raise ValueError('tar member contains identifying or unsupported metadata')
    return {'members_checked': len(members)}


class DistributionContractTestCase(unittest.TestCase):
    def test_source_normalizer_removes_pax_owner_and_gzip_metadata(self) -> None:
        normalize = runpy.run_path(str(ROOT / 'scripts/build_sdist.py'))['normalize_sdist']
        with tempfile.TemporaryDirectory(prefix='qingtian-header-test-') as temporary:
            archive = Path(temporary) / 'sample.tar.gz'
            name = 'sample/' + 'x' * 110 + '/资料.txt'
            payload = b'generic public payload\n'
            with archive.open('wb') as raw:
                with gzip.GzipFile(filename='synthetic-builder', mode='wb', fileobj=raw, mtime=123) as compressed:
                    with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as handle:
                        member = tarfile.TarInfo(name)
                        member.uid, member.gid = 12345, 23456
                        member.uname, member.gname = 'synthetic-builder', 'synthetic-group'
                        member.mode, member.mtime, member.size = 0o755, 123, len(payload)
                        member.pax_headers = {'uname': 'synthetic-builder', 'gname': 'synthetic-group',
                                              'uid': '12345', 'gid': '23456', 'atime': '123',
                                              'SCHILY.xattr.user.note': 'synthetic-private'}
                        handle.addfile(member, io.BytesIO(payload))
            with self.assertRaises(ValueError):
                assert_public_archive_headers(archive)
            normalize(archive)
            self.assertEqual(assert_public_archive_headers(archive), {'members_checked': 1})
            with tarfile.open(archive) as handle:
                member = handle.getmembers()[0]
                self.assertEqual(member.name, name)
                self.assertEqual(member.mode, 0o755)
                self.assertEqual(handle.extractfile(member).read(), payload)
            first = archive.read_bytes()
            normalize(archive)
            self.assertEqual(archive.read_bytes(), first)

    def test_source_normalizer_fails_closed_without_replacing_original(self) -> None:
        normalize = runpy.run_path(str(ROOT / 'scripts/build_sdist.py'))['normalize_sdist']
        with tempfile.TemporaryDirectory(prefix='qingtian-header-reject-') as temporary:
            archive = Path(temporary) / 'sample.tar.gz'
            with tarfile.open(archive, 'w:gz') as handle:
                member = tarfile.TarInfo('sample/link')
                member.type, member.linkname = tarfile.SYMTYPE, 'target'
                handle.addfile(member)
            original = archive.read_bytes()
            with self.assertRaises(ValueError):
                normalize(archive)
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(list(Path(temporary).iterdir()), [archive])

    def test_optional_pdf_and_build_dependency_floors(self) -> None:
        from packaging.requirements import Requirement
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        self.assertEqual(project['project']['dependencies'], [])
        pdf = Requirement(project['project']['optional-dependencies']['pdf'][0])
        self.assertEqual(pdf.name, 'pypdf')
        for old in ('6.0', '6.14.2', '6.15.0', '6.17.0'):
            self.assertNotIn(old, pdf.specifier)
        self.assertIn('6.18.0', pdf.specifier)
        self.assertNotIn('7.0', pdf.specifier)
        for group in (project['build-system']['requires'], project['project']['optional-dependencies']['dev']):
            requirement = next(Requirement(raw) for raw in group if Requirement(raw).name == 'setuptools')
            self.assertNotIn('77.0.3', requirement.specifier)
            self.assertIn('78.1.1', requirement.specifier)
            self.assertIn('84.0.0', requirement.specifier)

    def test_showcase_git_provenance_is_optional_and_root_scoped(self) -> None:
        spec = importlib.util.spec_from_file_location('showcase_bridge', ROOT / 'examples/showcase/engine_bridge.py')
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'path', sys.path.copy()):
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='qingtian-provenance-test-') as temporary:
            root = Path(temporary).resolve()
            with patch.object(module.subprocess, 'run') as invoke:
                self.assertIsNone(module.read_source_commit(root))
                invoke.assert_not_called()
            (root / '.git').mkdir()
            commit = '1' * 40
            with patch.object(module.subprocess, 'run') as invoke:
                invoke.return_value = subprocess.CompletedProcess([], 0, stdout=f'{root}\n{commit}\n')
                with patch.dict(os.environ, {'GIT_DIR': '/synthetic/unrelated/git'}):
                    self.assertEqual(module.read_source_commit(root), commit)
                self.assertNotIn('GIT_DIR', invoke.call_args.kwargs['env'])
                self.assertEqual(invoke.call_args.kwargs['timeout'], 5)
                invoke.return_value.stdout = f'{root.parent}\n{commit}\n'
                self.assertIsNone(module.read_source_commit(root))
                invoke.return_value.stdout = f'{root}\nnot-a-commit\n'
                self.assertIsNone(module.read_source_commit(root))
            for failure in (FileNotFoundError(), subprocess.CalledProcessError(128, 'git'),
                            subprocess.TimeoutExpired('git', 5)):
                with patch.object(module.subprocess, 'run', side_effect=failure):
                    self.assertIsNone(module.read_source_commit(root))

    def test_showcase_javascript_syntax_without_running_recorder(self) -> None:
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is needed only for showcase source syntax checks')
        for script in sorted((ROOT / 'examples/showcase').glob('*.cjs')):
            result = subprocess.run([node, '--check', str(script)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_allowlist_covers_public_tests_docs_and_showcase(self) -> None:
        declared = json.loads((ROOT / 'release-allowlist.json').read_text())['paths']
        self.assertEqual(len(declared), len(set(declared)))
        for relative in declared:
            self.assertTrue((ROOT / relative).is_file(), relative)
            self.assertFalse((ROOT / relative).is_symlink(), relative)
        for directory in ('tests', 'docs', 'examples/showcase'):
            expected = {
                str(path.relative_to(ROOT)) for path in (ROOT / directory).rglob('*')
                if path.is_file() and path.name != '.DS_Store'
                and '__pycache__' not in path.parts
            }
            self.assertEqual(expected, {p for p in declared if p.startswith(directory + '/')})
        self.assertEqual({str(p.relative_to(ROOT / 'examples/showcase'))
                          for p in (ROOT / 'examples/showcase').iterdir() if p.is_file()}, SHOWCASE_FILES)
        self.assertEqual({p for p in declared if p.startswith('docs/assets/')}, SHOWCASE_ASSETS)
        self.assertEqual(missing_relative_links(ROOT), [])

    def test_version_declarations_match_project_metadata(self) -> None:
        version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
        self.assertIn(version, (ROOT / 'CHANGELOG.md').read_text())
        for package in ('qingtian_core', 'qingtian_engine', 'qingtian_kb'):
            declarations = [node for node in ast.parse((ROOT / package / '__init__.py').read_text()).body
                            if isinstance(node, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)]
            self.assertEqual(len(declarations), 1)
            self.assertEqual(ast.literal_eval(declarations[0].value), version, package)

    def test_approved_video_bytes_and_embedded_fonts_are_preserved(self) -> None:
        notices = (ROOT / 'docs/diagrams/FONT-LICENSES.md').read_text()
        self.assertIn('SIL OPEN FONT LICENSE Version 1.1', notices)
        for holder in ('Excalidraw', 'LXGW', 'Nozomi Seto'):
            self.assertIn(holder, notices)
        video = ROOT / 'docs/assets/showcase/qingtian-showcase-short.mp4'
        self.assertEqual(video.stat().st_size, 6549535)
        self.assertEqual(sha256(video.read_bytes()).hexdigest(), APPROVED_VIDEO_SHA256)
        for name in ('engine-overview', 'engine-lifecycle'):
            scene = json.loads((ROOT / f'docs/diagrams/{name}.excalidraw').read_text())
            self.assertEqual(scene['type'], 'excalidraw')
            self.assertTrue(scene['elements'])
            svg = (ROOT / f'docs/diagrams/{name}.svg').read_text()
            for family in ('Excalifont', 'Xiaolai'):
                self.assertIn(f'font-family: {family}', svg)
            self.assertIn('url(data:font/woff2;base64,', svg)
            self.assertNotRegex(svg, r'url\([\s\'"]*https?://')
            diagnostics = json.loads((ROOT / f'docs/diagrams/{name}.font-diagnostics.json').read_text())
            self.assertTrue(diagnostics['checks']['excalifont'])
            self.assertTrue(diagnostics['checks']['xiaolai'])

    def test_sdist_and_independent_bundle_preserve_contract(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec('setuptools'), 'Install the dev extra before distribution tests')
        declared = json.loads((ROOT / 'release-allowlist.json').read_text())['paths']
        with tempfile.TemporaryDirectory(prefix='qingtian-source-contract-') as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            for relative in declared:
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            # Broad media/example globs must not pick up subsequent unreviewed output.
            for relative in ('docs/assets/showcase/unapproved.png', 'docs/assets/showcase/full.mp4',
                             'examples/showcase/unapproved.json', 'examples/showcase/private.md',
                             'output/private.md'):
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('synthetic unreviewed sentinel\n')
            environment = {k: v for k, v in os.environ.items()
                           if k != 'PYTHONPATH' and not k.startswith('QINGTIAN_')}
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            built = subprocess.run(
                [sys.executable, '-c', 'from scripts.build_sdist import build_sdist; build_sdist("dist")'],
                cwd=source, env=environment, capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            sdist = next((source / 'dist').glob('*.tar.gz'))
            bundle = root / 'allowlist.tar.gz'
            built_bundle = build_bundle(source, bundle)
            self.assertEqual(verify_bundle(bundle)['sha256'], built_bundle['sha256'])
            for kind, archive in (('sdist', sdist), ('bundle', bundle)):
                with self.subTest(kind=kind):
                    self.assertGreater(assert_public_archive_headers(archive)['members_checked'], 0)
                    payloads = archive_files(archive)
                    if kind == 'sdist':
                        version = tomllib.loads((source / 'pyproject.toml').read_text())['project']['version']
                        self.assertIn(f'Version: {version}\n', payloads['PKG-INFO'].decode())
                        from email.parser import BytesParser
                        from packaging.requirements import Requirement
                        metadata = BytesParser().parsebytes(payloads['PKG-INFO'])
                        requirement = next(Requirement(raw) for raw in metadata.get_all('Requires-Dist')
                                           if Requirement(raw).name == 'pypdf')
                        self.assertNotIn('6.14.2', requirement.specifier)
                        self.assertNotIn('6.17.0', requirement.specifier)
                        self.assertIn('6.18.0', requirement.specifier)
                    self.assertFalse(set(declared) - payloads.keys(), sorted(set(declared) - payloads.keys()))
                    for relative in declared:
                        self.assertEqual(payloads[relative], (source / relative).read_bytes(), relative)
                    self.assertEqual({p for p in payloads if p.startswith('docs/assets/')}, SHOWCASE_ASSETS)
                    self.assertEqual({p.removeprefix('examples/showcase/') for p in payloads
                                      if p.startswith('examples/showcase/')}, SHOWCASE_FILES)
                    self.assertFalse(any(p.startswith('output/') for p in payloads))
                    self.assertFalse(any(p.endswith(('.webm', '.pptx', '.db', '.sqlite3')) for p in payloads))
                    destination = root / kind
                    destination.mkdir()
                    with tarfile.open(archive) as handle:
                        handle.extractall(destination, filter='data')
                    extracted = next(destination.iterdir())
                    self.assertFalse((extracted / '.git').exists())
                    for launcher in ('qingtian', 'qingtian-kb'):
                        self.assertEqual((extracted / launcher).stat().st_mode & 0o777,
                                         (source / launcher).stat().st_mode & 0o777)
                    self.assertEqual(missing_relative_links(extracted), [])
                    # CLI parsing only; --help returns before imports, DB creation or recording.
                    for script in ('engine_bridge.py', 'verify_recording.py'):
                        help_result = subprocess.run([sys.executable, str(extracted / 'examples/showcase' / script), '--help'],
                                                     cwd=root, env=environment, capture_output=True, text=True, timeout=10)
                        self.assertEqual(help_result.returncode, 0, help_result.stderr)
                    for script in (extracted / 'examples/showcase').glob('*.py'):
                        ast.parse(script.read_text(), filename=str(script))
                    provenance = subprocess.run(
                        [sys.executable, '-c',
                         'import json, runpy, sys; print(json.dumps(runpy.run_path(sys.argv[1])["read_source_commit"]()))',
                         str(extracted / 'examples/showcase/engine_bridge.py')],
                        cwd=root, env=environment, capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(provenance.returncode, 0, provenance.stderr)
                    self.assertIsNone(json.loads(provenance.stdout))


if __name__ == '__main__':
    unittest.main()
