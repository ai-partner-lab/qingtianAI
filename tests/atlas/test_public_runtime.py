from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.resources import files
from unittest.mock import Mock, patch

from qingtian_engine import cli, config
from qingtian_engine.multipart import (
    MAX_PARTS, MultipartError, parse_multipart, read_body,
)


def encoded_form(parts, boundary="qingtian-runtime-test"):
    body = bytearray()
    for headers, content in parts:
        body.extend(("--" + boundary + "\r\n").encode())
        body.extend(headers)
        body.extend(b"\r\n\r\n" + content + b"\r\n")
    body.extend(("--" + boundary + "--\r\n").encode())
    return "multipart/form-data; boundary=" + boundary, bytes(body)


def field(name, value):
    return ('Content-Disposition: form-data; name="{}"'.format(name).encode(), value)


class PublicRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qingtian-public-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        environment = patch.dict(os.environ, {
            "QINGTIAN_ENGINE_HOME": str(self.root / "state"),
            "QINGTIAN_KNOWLEDGE_CONFIG": str(self.root / "no-knowledge.json"),
            "QINGTIAN_PROJECTS_CONFIG": str(self.root / "no-projects.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_default_state_is_under_user_home_not_package(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=self.root):
            state = config.default_data_dir()
        self.assertEqual(self.root / ".local/share/qingtian/engine", state)
        self.assertFalse(state.exists())
        self.assertFalse(state.is_relative_to(config.PACKAGE_DIR))

    def test_explicit_environment_state_is_resolved_dynamically(self):
        self.assertEqual(self.root / "state", config.default_data_dir())
        with patch.dict(os.environ, {"QINGTIAN_ENGINE_HOME": ""}):
            with self.assertRaises(ValueError):
                config.default_data_dir()

    def test_default_workspace_is_cwd_not_package_parent(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "cwd", return_value=self.root):
            self.assertEqual(self.root, config.default_workspace())

    def test_policy_is_a_packaged_resource(self):
        resource = files("qingtian_engine").joinpath("resources/policy.json")
        self.assertTrue(resource.is_file())
        self.assertEqual(json.loads(resource.read_text(encoding="utf-8")), config.load_policy())

    def test_policy_explicit_path_remains_supported(self):
        path = self.root / "policy.json"
        path.write_text('{"fixture":true}', encoding="utf-8")
        self.assertEqual({"fixture": True}, config.load_policy(path))

    def test_ensure_data_dirs_never_uses_package_for_default_state(self):
        paths = config.ensure_data_dirs()
        self.assertEqual(self.root / "state/config", paths["config"])
        self.assertEqual(self.root / "state/control-plane.sqlite3", paths["db"])
        self.assertTrue(paths["config"].is_dir())
        self.assertFalse(paths["db"].exists())

    def test_all_server_entrypoints_default_to_8766_manual(self):
        parser = cli.configure_parser()
        for command in ("start", "bootstrap", "demo", "status"):
            args = parser.parse_args([command])
            self.assertEqual(8766, args.port)
            if hasattr(args, "mode"):
                self.assertEqual("manual", args.mode)

    def test_doctor_is_read_only_and_missing_codex_is_not_a_start_blocker(self):
        state = self.root / "not-created"
        environment_before = dict(os.environ)
        with patch.object(cli, "_which", return_value=None), patch.object(cli, "build_service") as build, patch(
            "subprocess.Popen", side_effect=AssertionError("doctor must not spawn")
        ), patch.object(cli.urllib.request, "urlopen", side_effect=AssertionError("doctor must not connect")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, cli.main(["--data-dir", str(state), "--workspace", str(self.root), "doctor"]))
        payload = json.loads(output.getvalue())
        build.assert_not_called()
        self.assertTrue(payload["read_only"])
        self.assertTrue(payload["dashboard_ready"])
        self.assertFalse(payload["credentials_checked"])
        self.assertFalse(payload["credentials_required_to_start"])
        self.assertEqual({"codex": False, "git": False}, payload["optional_tools"])
        self.assertFalse(state.exists())
        self.assertEqual(environment_before, dict(os.environ))

    def test_bootstrap_without_execution_tools_still_initializes_and_can_start(self):
        with patch.object(cli, "_which", return_value=None), patch.object(cli, "start_server", return_value=0) as start, redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main([
                "--data-dir", str(self.root / "bootstrap"), "--workspace", str(self.root),
                "bootstrap", "--start",
            ]))
        self.assertEqual("manual", start.call_args.args[4])
        self.assertEqual(8766, start.call_args.args[1])

    def test_task_project_and_repo_are_mutually_exclusive(self):
        parser = cli.configure_parser()
        args = parser.parse_args(["task", "add", "--title", "Sample", "--project", "sample"])
        self.assertEqual("sample", args.project)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["task", "add", "--title", "Sample", "--project", "sample", "--repo", "/unused"])

    def test_project_list_does_not_initialize_engine_database(self):
        from qingtian_engine.project_config import load_project_config
        state = self.root / "list-only"
        empty = load_project_config(data_dir=state)
        with patch.object(cli, "build_service") as build, redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(["--data-dir", str(state), "project", "list"]))
        build.assert_not_called()
        self.assertFalse(state.exists())
        self.assertIsInstance(empty.to_dict(), dict)

    def test_project_register_list_remove_changes_only_registry(self):
        state = self.root / "registry-state"
        repository = self.root / "sample-repository"
        (repository / ".git").mkdir(parents=True)
        registry = state / "config/projects.local.json"
        with patch.dict(os.environ, {"QINGTIAN_PROJECTS_CONFIG": str(registry)}), patch(
            "subprocess.Popen", side_effect=AssertionError("registration must not execute tools")
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, cli.main([
                "--data-dir", str(state), "project", "register", "sample", "--repo", str(repository),
                "--base", "dev", "--role", "backend",
            ]))
            registered = json.loads(output.getvalue())
            self.assertEqual(str(repository), registered["projects"]["sample"]["repository"])
            self.assertEqual(["backend"], registered["projects"]["sample"]["roles"])
            output.seek(0)
            output.truncate()
            self.assertEqual(0, cli.main(["--data-dir", str(state), "project", "list"]))
            self.assertIn("sample", json.loads(output.getvalue())["projects"])
            output.seek(0)
            output.truncate()
            self.assertEqual(0, cli.main(["--data-dir", str(state), "project", "remove", "sample"]))
            self.assertEqual({}, json.loads(output.getvalue())["projects"])
        self.assertTrue((repository / ".git").is_dir())
        self.assertEqual(0o600, registry.stat().st_mode & 0o777)
        self.assertFalse((state / "control-plane.sqlite3").exists())

    def test_invalid_project_registration_returns_structured_error(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(2, cli.main([
                "project", "register", "sample", "--repo", str(self.root / "missing-repo"), "--base", "dev",
            ]))
        self.assertEqual("invalid-project-registration", json.loads(output.getvalue())["code"])


class MultipartRuntimeTest(unittest.TestCase):
    def test_utf8_fields_and_binary_attachment_are_preserved(self):
        content = b"\x89PNG\r\n\x1a\n\xff\x00binary\r\n"
        kind, body = encoded_form([
            field("text", "中英文字段".encode()),
            field("empty", b""),
            (b'Content-Disposition: form-data; name="attachments"; filename="screen.png"\r\nContent-Type: image/png', content),
        ])
        form = parse_multipart(kind, body)
        self.assertEqual({"text": "中英文字段", "empty": ""}, form.fields)
        self.assertEqual("screen.png", form.files[0].filename)
        self.assertEqual("image/png", form.files[0].content_type)
        self.assertEqual(content, form.files[0].stream.read())
        form.close()
        self.assertTrue(form.files[0].stream.closed)

    def test_quoted_boundary_and_closing_without_final_newline(self):
        kind, body = encoded_form([field("text", b"fixture")])
        kind = 'multipart/form-data; boundary="qingtian-runtime-test"'
        self.assertEqual("fixture", parse_multipart(kind, body[:-2]).fields["text"])

    def test_unicode_attachment_filename_is_preserved(self):
        header = 'Content-Disposition: form-data; name="attachments"; filename="参考.png"\r\nContent-Type: image/png'.encode()
        kind, body = encoded_form([(header, b"\x89PNG")])
        form = parse_multipart(kind, body)
        self.assertEqual("参考.png", form.files[0].filename)
        form.close()

    def test_empty_form_is_parseable(self):
        self.assertEqual({}, parse_multipart("multipart/form-data; boundary=empty", b"--empty--\r\n").fields)

    def test_invalid_boundary_and_content_type_are_rejected(self):
        for kind in ("application/json", "multipart/form-data", "multipart/form-data; boundary=", "multipart/form-data; boundary=x\r\nX-Header: injected"):
            with self.subTest(kind=kind), self.assertRaises(MultipartError):
                parse_multipart(kind, b"--x--\r\n")

    def test_truncated_multipart_is_rejected(self):
        kind, body = encoded_form([field("text", b"fixture")])
        with self.assertRaises(MultipartError):
            parse_multipart(kind, body[:-10])

    def test_duplicate_fields_are_not_silently_overwritten(self):
        kind, body = encoded_form([field("intent", b"analyze"), field("intent", b"implement")])
        with self.assertRaisesRegex(MultipartError, "Duplicate multipart field"):
            parse_multipart(kind, body)

    def test_part_count_is_bounded_before_mime_decoding(self):
        kind, body = encoded_form([field("field" + str(i), b"x") for i in range(MAX_PARTS + 1)])
        with self.assertRaises(MultipartError) as error:
            parse_multipart(kind, body)
        self.assertEqual(413, error.exception.status)

    def test_nested_multipart_and_transfer_encoding_are_rejected(self):
        for extra in (b"Content-Type: multipart/mixed; boundary=inner", b"Content-Transfer-Encoding: base64"):
            kind, body = encoded_form([(b'Content-Disposition: form-data; name="text"\r\n' + extra, b"fixture")])
            with self.assertRaises(MultipartError):
                parse_multipart(kind, body)

    def test_non_utf8_form_fields_are_rejected(self):
        kind, body = encoded_form([field("text", b"\xff\x00")])
        with self.assertRaisesRegex(MultipartError, "UTF-8"):
            parse_multipart(kind, body)

    def test_oversize_length_is_rejected_without_reading(self):
        stream = Mock()
        with self.assertRaises(MultipartError) as error:
            read_body(stream, "100", 20)
        self.assertEqual(413, error.exception.status)
        stream.read.assert_not_called()

    def test_negative_malformed_and_truncated_lengths_are_rejected(self):
        for length in ("-1", "1.0", " ", "+1"):
            with self.subTest(length=length), self.assertRaises(MultipartError):
                read_body(io.BytesIO(b"abc"), length, 10)
        with self.assertRaisesRegex(MultipartError, "Truncated"):
            read_body(io.BytesIO(b"abc"), "4", 10)

    def test_body_reader_consumes_only_declared_length(self):
        stream = io.BytesIO(b"abcNEXT")
        self.assertEqual(b"abc", read_body(stream, "3", 10))
        self.assertEqual(b"NEXT", stream.read())


if __name__ == "__main__":
    unittest.main()
