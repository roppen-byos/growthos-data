from contextlib import redirect_stdout
import io
import json
import unittest

from gwosdata.errors import SafeParser, execute_cli, reject


class SafeOutputTests(unittest.TestCase):
    def invoke(self, operation, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            code = execute_cli(operation, **kwargs)
        return code, json.loads(output.getvalue())

    def test_success_and_reviewed_errors(self):
        self.assertEqual(self.invoke(lambda: {"ok": True}), (0, {"ok": True}))
        code, output = self.invoke(lambda: reject("ARGUMENTS", "Reviewed message."))
        self.assertEqual((code, output["error"]["message"]), (2, "Reviewed message."))

    def test_unexpected_errors_and_parser_inputs_do_not_leak(self):
        def fail():
            raise ValueError("private-canary")
        code, output = self.invoke(fail, failure_code="MIGRATION_FAILED")
        self.assertEqual((code, output["error"]["code"]), (2, "MIGRATION_FAILED"))
        self.assertNotIn("private-canary", str(output))
        code, output = self.invoke(lambda: SafeParser().parse_args(["--private-canary"]))
        self.assertEqual(code, 2)
        self.assertNotIn("private-canary", str(output))

    def test_output_is_bounded_without_partial_result_and_cancellation_is_sanitized(self):
        code, output = self.invoke(lambda: {"large": "private-canary" * 20}, max_bytes=50)
        self.assertEqual((code, output["error"]["code"]), (2, "OUTPUT_BOUND"))
        self.assertNotIn("private-canary", str(output))
        def cancel():
            raise KeyboardInterrupt()
        code, output = self.invoke(cancel)
        self.assertEqual((code, output["error"]["code"]), (130, "CANCELLED"))
