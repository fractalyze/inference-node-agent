"""Locate external test binaries (alloy, prometheus, docker).

A missing binary skips the test, unless REQUIRE_TOOLS=1 (CI), where it fails.
"""

import os
import shutil
import unittest


def find(env: str, name: str) -> str | None:
    return os.environ.get(env) or shutil.which(name)


def require(test: unittest.TestCase, env: str, name: str) -> str:
    path = find(env, name)
    if path:
        return path
    if os.environ.get("REQUIRE_TOOLS") == "1":
        test.fail(f"{name} not found and REQUIRE_TOOLS=1; set {env}")
    test.skipTest(f"{name} not found; set {env}")
