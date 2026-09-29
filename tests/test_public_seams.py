"""Public transport, validator and runner seams that tests patch (P0-26, issue #30).

Tests patch these public names instead of private ones, so ADR-0001 module moves
can keep the seams without rewriting tests. The old private names stay as aliases
until every caller has migrated; patching an alias has no effect on the code.
"""

import unittest
from unittest.mock import patch

from jevtrader import bars, launchd, local, providers, sec, secrets

SEAMS = {
    providers: {"post_json": "_post_json"},
    local: {"http_json": "_http_json", "normalize_base_url": "_base_url"},
    bars: {"urlopen": "_urlopen", "validate_url": "_validate_url"},
    sec: {"urlopen": "_transport", "validate_url": "_validate_url", "utc_now": "_utc_now"},
    secrets: {"run": "_run", "keychain_available": "_keychain_available"},
    launchd: {"run": "_run"},
}


class PublicSeamTests(unittest.TestCase):
    def test_public_seams_exist_and_private_aliases_match(self):
        for module, names in SEAMS.items():
            for public, private in names.items():
                with self.subTest(module=module.__name__, name=public):
                    self.assertTrue(callable(getattr(module, public)))
                    # conftest may have patched the public seam, so compare the alias
                    # with the original function by name rather than identity.
                    alias = getattr(module, private)
                    self.assertEqual((alias.__module__, alias.__name__), (module.__name__, public))

    def test_providers_route_through_public_post_json(self):
        with patch.object(providers, "post_json", side_effect=providers.ProviderError("seam")):
            with self.assertRaisesRegex(providers.ProviderError, "seam"):
                providers._request(None, "https://example.test", {}, "k")

    def test_bars_client_uses_public_validator_and_transport(self):
        calls = []
        with (
            patch.dict("os.environ", {bars.KEY_ID: "id", bars.SECRET_KEY: "secret"}),
            patch.object(bars, "validate_url", side_effect=lambda url: calls.append(url)),
            patch.object(bars, "urlopen", side_effect=bars.BarsError("seam")),
            patch.object(bars._LIMITER, "acquire"),
        ):
            with self.assertRaisesRegex(bars.BarsError, "seam"):
                bars._Client(None).get("example.test", "/x", {"a": "b"})
        self.assertEqual(calls, ["https://example.test/x?a=b"])

    def test_sec_client_uses_public_validator_transport_and_clock(self):
        with (
            patch.object(sec, "urlopen") as transport,
            patch.object(sec, "utc_now") as now,
            patch.object(sec, "validate_url", side_effect=sec.SECError("seam")),
        ):
            client = sec._SECClient("Lab lab@example.test", 5, 1)
            self.assertIs(client.transport, transport)
            self.assertIs(client.now, now)
            with self.assertRaisesRegex(sec.SECError, "seam"):
                client.get("https://www.sec.gov/files/company_tickers.json")
        transport.assert_not_called()

    def test_local_routes_through_public_base_url(self):
        with patch.object(local, "normalize_base_url", side_effect=ValueError("seam")):
            with self.assertRaisesRegex(ValueError, "seam"):
                local.http_json("http://127.0.0.1:1234/v1/models", None, "", 5)

    def test_secrets_and_launchd_default_runners_are_public(self):
        with (
            patch.object(secrets, "keychain_available", return_value=True),
            patch.object(secrets, "run") as run,
        ):
            self.assertIs(secrets._require_keychain("ALPACA_API_KEY_ID"), run)
        with (
            patch.object(launchd.sys, "platform", "darwin"),
            patch.object(launchd.os.path, "exists", return_value=True),
            patch.object(launchd, "run") as run,
        ):
            self.assertIs(launchd._runner(None), run)


if __name__ == "__main__":
    unittest.main()
