"""R4 guard (I-4): no order, position or account route and no broker host outside execution/.

The scan reads every source file of the package, except under `jevtrader/execution/`, which is
the only place broker code may live (ADR-0001). It fails on any string that names a broker
trading host or an order, position or account route.

One exemption exists today: `bars.py` reads `/v2/calendar` from Alpaca's paper trading host,
because the market-data host has no calendar. The exemption is pinned to that one file, that
one host and that one route; `test_calendar_host_serves_only_the_calendar` checks the route
table, so any other route on that host still fails. Removing the exemption needs either
data-only Alpaca credentials or a calendar source off the trading host (P0-04, owner action).
"""

import ast
import re
import unittest
from pathlib import Path
from urllib.parse import urlencode

from jevtrader import bars

PACKAGE = Path(bars.__file__).resolve().parent
EXECUTION = PACKAGE / "execution"
SOURCE_SUFFIXES = (".py", ".json", ".toml", ".cfg", ".txt", ".html", ".js")

BROKER_HOST = re.compile(
    r"(?<![\w.-])(?:"
    r"(?:paper-)?api\.alpaca\.markets"
    r"|broker-api(?:\.sandbox)?\.alpaca\.markets"
    r"|(?:sandbox\.)?api\.tradier\.com"
    r"|api\.tastytrade\.com|api\.tastyworks\.com"
    r"|api\.schwabapi\.com"
    r"|api\.(?:sandbox\.)?etrade\.com"
    r"|api\.robinhood\.com"
    r"|api\.ibkr\.com|api\.interactivebrokers\.com"
    r"|api-fxtrade\.oanda\.com|api-fxpractice\.oanda\.com"
    r")(?![\w-])",
    re.IGNORECASE,
)
# A URL path segment that addresses orders, positions or accounts, e.g. /v2/orders,
# /v2/positions/AAPL, /v2/account, /v1/trading/accounts/{id}/orders, /iserver/account.
ORDER_ROUTE = re.compile(
    r"(?:^|/)(?:orders?|positions?|accounts?|iserver|portfolio|fills|trades?)(?:/|\?|$)",
    re.IGNORECASE,
)
# Exactly one allowed use of a broker host, with the only route it may serve.
EXEMPT = {("bars.py", "paper-api.alpaca.markets")}
EXEMPT_ROUTES = {("paper-api.alpaca.markets", "/v2/calendar")}


def _is_path_like(text: str) -> bool:
    # Any space-free string with a slash, so relative routes such as "v2/orders" (joined
    # onto a base URL) count as well as absolute ones.
    stripped = text.strip()
    return "://" in stripped or ("/" in stripped and not any(c.isspace() for c in stripped))


def _route_of(text: str) -> str:
    return re.sub(r"^[a-z]+://[^/]*", "", text.strip(), flags=re.IGNORECASE)


def _fold(node: ast.AST) -> str:
    """The string a concatenation or f-string builds, with "{}" for unknown parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _fold(node.left) + _fold(node.right)
    if isinstance(node, ast.FormattedValue):
        return _fold(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(_fold(value) for value in node.values)
    return "{}"


def violations(name: str, source: str) -> list[str]:
    """Broker hosts and order, position or account routes named in one source file."""
    found = []
    for match in BROKER_HOST.finditer(source):
        host = match.group(0).lower()
        if (name, host) not in EXEMPT:
            line = source.count("\n", 0, match.start()) + 1
            found.append(f"{name}:{line}: broker host {host}")
    if name.endswith(".py"):
        tree = ast.parse(source)
        strings = [
            (node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        # Routes assembled from pieces ("/v2/" + "orders", f"{base}/v2/{kind}") are folded,
        # with "{}" standing in for any part that is not a string literal.
        strings += [
            (node.lineno, _fold(node))
            for node in ast.walk(tree)
            if isinstance(node, (ast.BinOp, ast.JoinedStr))
        ]
        if name != "bars.py":
            # The exempt host may not leave bars.py under its constant's name either.
            found += [
                f"{name}:{node.lineno}: exempt calendar host reused"
                for node in ast.walk(tree)
                if (isinstance(node, ast.Name) and node.id == "CALENDAR_HOST")
                or (isinstance(node, ast.Attribute) and node.attr == "CALENDAR_HOST")
            ]
    else:
        strings = [(number, line) for number, line in enumerate(source.splitlines(), 1)]
        strings = [
            (number, token)
            for number, line in strings
            for token in re.findall(r"""[^\s"'`<>()]+""", line)
        ]
    for line, text in strings:
        if _is_path_like(text) and ORDER_ROUTE.search(_route_of(text)):
            found.append(f"{name}:{line}: order, position or account route {text!r}")
    return found


def package_sources() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE.rglob("*")
        if path.is_file()
        and path.suffix in SOURCE_SUFFIXES
        and "__pycache__" not in path.parts
        and EXECUTION not in path.parents
    )


class NoOrderRouteTests(unittest.TestCase):
    def test_package_names_no_order_route_or_broker_host(self):
        sources = package_sources()
        self.assertIn(PACKAGE / "bars.py", sources)
        found = []
        for path in sources:
            name = path.relative_to(PACKAGE).as_posix()
            found += violations(name, path.read_text(encoding="utf-8"))
        self.assertEqual(found, [])

    def test_calendar_host_serves_only_the_calendar(self):
        broker_routes = {(host, path) for host, path in bars._ROUTES if BROKER_HOST.fullmatch(host)}
        self.assertEqual(broker_routes, EXEMPT_ROUTES)
        self.assertEqual(bars.CALENDAR_HOST, "paper-api.alpaca.markets")
        self.assertEqual(bars.DATA_HOST, "data.alpaca.markets")

    def test_validator_refuses_order_position_and_account_routes(self):
        for host in ("paper-api.alpaca.markets", "api.alpaca.markets", bars.DATA_HOST):
            for path in (
                "/v2/orders",
                "/v2/orders/abc",
                "/v2/positions",
                "/v2/positions/AAPL",
                "/v2/account",
                "/v2/account/configurations",
                "/v2/account/activities",
            ):
                url = f"https://{host}{path}?{urlencode({'symbol': 'AAPL', 'qty': '1'})}"
                with self.subTest(url=url), self.assertRaises(bars.BarsError):
                    bars.validate_url(url)


class GuardSelfTests(unittest.TestCase):
    """The scanner must catch planted violations, or the guard proves nothing."""

    def test_catches_broker_hosts(self):
        for host in (
            "api.alpaca.markets",
            "paper-api.alpaca.markets",
            "broker-api.sandbox.alpaca.markets",
            "API.Alpaca.Markets",
            "api.tradier.com",
            "api.schwabapi.com",
        ):
            source = f"HOST = {host!r}\n"
            with self.subTest(host=host):
                self.assertTrue(violations("feeds.py", source))

    def test_catches_order_position_and_account_routes(self):
        for route in (
            "/v2/orders",
            "/v2/orders/{order_id}",
            "/v2/positions",
            "/v2/positions/AAPL",
            "/v2/account",
            "/v2/account/activities",
            "/v1/trading/accounts/x/orders",
            "https://example.test/v1/iserver/account",
            "/v2/orders?status=open",
        ):
            with self.subTest(route=route):
                self.assertTrue(violations("feeds.py", f"PATH = {route!r}\n"))
                self.assertTrue(violations("feeds.py", f"x = f'{route}{{y}}'\n"))
                self.assertTrue(violations("page.html", f'<a href="{route}">x</a>\n'))

    def test_catches_relative_routes(self):
        for source in (
            'PATH = "v2/orders"\n',
            'url = urljoin(BASE, "v2/positions")\n',
            'url = BASE + "v2/account/activities"\n',
        ):
            with self.subTest(source=source):
                self.assertTrue(violations("feeds.py", source))

    def test_catches_routes_built_by_concatenation(self):
        for source in (
            'url = "https://" + HOST + "/v2/" + "orders"\n',
            'url = BASE + "/v2/" + kind + "/" + "positions"\n',
            "url = f\"{BASE}/v2/{'acc' + 'ount'}\"\n",
            'url = "/v2/" + "acc" + "ount"\n',
        ):
            with self.subTest(source=source):
                self.assertTrue(violations("feeds.py", source))

    def test_calendar_host_constant_stays_in_bars(self):
        for source in (
            'url = "https://" + bars.CALENDAR_HOST + "/v2/" + "orders"\n',
            "from .bars import CALENDAR_HOST\nhost = CALENDAR_HOST\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(violations("web.py", source))
        self.assertEqual(violations("bars.py", "url = CALENDAR_HOST\n"), [])

    def test_exemption_is_bound_to_one_file(self):
        source = 'CALENDAR_HOST = "paper-api.alpaca.markets"\n'
        self.assertEqual(violations("bars.py", source), [])
        self.assertTrue(violations("web.py", source))

    def test_ignores_prose_and_other_hosts(self):
        source = (
            '"""It never places orders; positions are simulated."""\n'
            'HOST = "data.alpaca.markets"\n'
            'PATH = "/v2/stocks/bars"\n'
            'CAL = "/v2/calendar"\n'
            'x = "max_positions"\n'
        )
        self.assertEqual(violations("feeds.py", source), [])

    def test_execution_package_is_excluded_from_the_scan(self):
        self.assertNotIn(EXECUTION, [path.parent for path in package_sources()])


if __name__ == "__main__":
    unittest.main()
