from __future__ import annotations

import runpy
import unittest
from unittest import mock

from hpsync import cli as hpsync


class EntrypointTests(unittest.TestCase):
    def test_python_module_entrypoint_returns_the_cli_exit_code(self) -> None:
        with mock.patch.object(hpsync, "main", return_value=7):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_module("hpsync.__main__", run_name="__main__")

        self.assertEqual(stopped.exception.code, 7)


if __name__ == "__main__":
    unittest.main()
