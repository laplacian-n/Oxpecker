"""A 402/429 from OpenRouter maps to ProviderQuotaError (a pausable, operator-actionable state);
anything else stays a RuntimeError (a real failure). Pins the status-code split the driver relies
on to tell 'pause and resume' from 'crash'."""
from __future__ import annotations

import io
import unittest
import urllib.error

from .openrouter import ProviderQuotaError, _raise_http_error


def _err(code: int, body: bytes = b'{"error":{"message":"x"}}'):
    return urllib.error.HTTPError("u", code, "reason", {}, io.BytesIO(body))


class OpenRouterQuotaMappingTest(unittest.TestCase):
    def test_402_and_429_are_quota_errors_carrying_their_status(self):
        for code in (402, 429):
            with self.assertRaises(ProviderQuotaError) as ctx:
                _raise_http_error(_err(code))
            self.assertEqual(ctx.exception.status, code)

    def test_other_statuses_stay_runtime_errors(self):
        for code in (400, 404, 500, 503):
            with self.assertRaises(RuntimeError) as ctx:
                _raise_http_error(_err(code))
            self.assertNotIsInstance(ctx.exception, ProviderQuotaError)


if __name__ == "__main__":
    unittest.main()
