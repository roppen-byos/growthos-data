"""Reviewed failures and bounded operator CLI output."""

import argparse
import json
from typing import Callable, NoReturn

MAX_OUTPUT_BYTES = 131_072


class GrowthOSError(Exception):
    """Only fixed, reviewed messages may cross the tool boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def reject(code: str, message: str) -> NoReturn:
    raise GrowthOSError(code, message)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        reject("ARGUMENTS", "Unsupported or missing arguments; run --help for reviewed operations. Input values withheld.")


def execute_cli(
    fn: Callable[[], dict], *, max_bytes: int = MAX_OUTPUT_BYTES,
    failure_code: str = "OPERATION_FAILED",
) -> int:
    """Emit bounded JSON without exception text, tracebacks or partial results."""
    try:
        output = json.dumps(fn(), sort_keys=True, allow_nan=False, separators=(",", ":"))
        if len(output.encode()) > max_bytes:
            reject("OUTPUT_BOUND", "Evidence exceeds the output limit; narrow the request. No partial output emitted.")
    except GrowthOSError as error:
        print(json.dumps({"error": {"code": error.code, "message": error.message}}))
        return 130 if error.code == "CANCELLED" else 2
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "CANCELLED", "message": "Operation cancelled. If a write was in progress, inspect current data before restarting."}}))
        return 130
    except Exception:
        print(json.dumps({"error": {"code": failure_code, "message": "Operation failed; diagnostic details withheld."}}))
        return 2
    print(output)
    return 0
