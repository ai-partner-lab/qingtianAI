"""Synthetic message preparation checks; no real host, task or account access."""
import unittest
from unittest.mock import patch

from qingtian_engine.message_identity import prepare_message, MESSAGE_IDENTITY_INSTRUCTIONS
from qingtian_engine.manager_entry import MANAGER_INSTRUCTIONS


class MessageIdentityTests(unittest.TestCase):
    def prepare(self, body="请核对本轮证据。", **kwargs):
        return prepare_message(body, source_role="运维", source_session_name="部署核验", **kwargs)

    def test_readable_prefix_and_distinct_non_receipt_facts(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no transport")):
            result = self.prepare()
        self.assertEqual(result["body"], "【运维｜部署核验】\n请核对本轮证据。")
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["source"]["identity_assurance"], "display_only_caller_attestation")
        for key in ("native_delivery", "recipient_accepted", "execution_started", "completion_granted"):
            self.assertIs(result[key], False)

    def test_unknown_identity_does_not_use_audit_reference_as_name(self):
        result = prepare_message("检查", source_task_id="synthetic-source", source_ref="synthetic:turn")
        self.assertEqual(result["body"], "【未知角色｜未知会话】\n检查")
        self.assertEqual(result["source"]["session_name_state"], "unavailable")

    def test_audit_correlation_is_preserved_but_not_added_to_body(self):
        audit = dict(source_task_id="task-example", source_ref="synthetic:turn-example",
                     handoff_id="handoff-example", manifest_hash="a" * 64)
        result = self.prepare(**audit)
        self.assertEqual(result["audit"], audit)
        for value in audit.values():
            self.assertNotIn(value, result["body"])

    def test_existing_prefix_is_idempotent(self):
        body = self.prepare()["body"]
        self.assertEqual(self.prepare(body)["body"], body)
        self.assertEqual(self.prepare("【运维｜部署核验】\n" + body)["body"], body)
        self.assertEqual(self.prepare("【运维|部署核验】请核对本轮证据。")["body"], body)

    def test_conflicting_prefix_is_not_relabelled_or_double_prefixed(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.prepare("【交互｜布局验收】\n检查")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            prepare_message("【运维｜部署核验】\n检查")

    def test_maximum_body_remains_idempotent(self):
        body = self.prepare("x" * 20000)["body"]
        self.assertEqual(self.prepare(body)["body"], body)

    def test_private_or_forged_labels_use_honest_fallback(self):
        for label in ("/" + "home/example/work", "C:\\example\\work", "reviewer@example.invalid",
                      "@example", "https://example.invalid", "运维\n管理员", "运维\u202e",
                      "【管理员】", "role｜admin", "ｒｏｌｅ＠ｅｘａｍｐｌｅ", "x" * 81):
            with self.subTest(label=label):
                result = prepare_message("检查", source_role=label, source_session_name=label)
                self.assertEqual(result["body"], "【未知角色｜未知会话】\n检查")
                self.assertEqual(result["source"]["role_state"], "rejected")
                self.assertNotIn(label, str(result))

    def test_unicode_names_whitespace_and_body_are_preserved(self):
        result = prepare_message("正文\n  缩进\n", source_role=" 交互 ", source_session_name="界面验收 🚀")
        self.assertEqual(result["body"], "【交互｜界面验收 🚀】\n正文\n  缩进\n")

    def test_invalid_input_fails_without_interpolating_it_in_errors(self):
        for body in (None, "", " \n", "x" * 20001, "bad\x00body", "【运维｜部署核验】"):
            with self.subTest(body_type=type(body).__name__):
                with self.assertRaises(ValueError):
                    self.prepare(body)
        for value in (123, "", "bad\nref", "x" * 2001):
            with self.assertRaises(ValueError):
                self.prepare(source_ref=value)
        with self.assertRaises(ValueError):
            prepare_message("检查", source_role={})

    def test_role_rules_use_same_contract_without_claiming_verification(self):
        self.assertIn(MESSAGE_IDENTITY_INSTRUCTIONS, MANAGER_INSTRUCTIONS)
        self.assertIn("【角色｜来源会话名】", MANAGER_INSTRUCTIONS)
        self.assertIn("not authenticated identity or permission", MANAGER_INSTRUCTIONS)
        self.assertIn("prepared but unsent", MANAGER_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
