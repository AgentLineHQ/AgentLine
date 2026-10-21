"""The telephony router picks carriers without a database or a real carrier."""

import asyncio
import unittest

from agentline.providers.base import ProvisionedNumber
from agentline.providers.registry import register_telephony
from agentline.providers.router import RoutingError, TelephonyRouter, parse_routes


class _Carrier:
    name = "base"
    media = "twilio"
    configured = True
    fails = False

    def is_configured(self):
        return self.configured

    async def provision_number(self, country="US", number_type="local", area_code=None, pattern=None, agent_id=None):
        if self.fails:
            raise RuntimeError(f"{self.name} has no numbers")
        return ProvisionedNumber(provider_id=f"{self.name}-id", phone_number="+15550001111")


class _Alpha(_Carrier):
    name = "alpha"


class _Beta(_Carrier):
    name = "beta"


class _Broken(_Carrier):
    name = "broken"
    fails = True


class _Offline(_Carrier):
    name = "offline"
    configured = False


register_telephony("alpha", _Alpha)
register_telephony("beta", _Beta)
register_telephony("broken", _Broken)
register_telephony("offline", _Offline)


def _router(**kwargs):
    kwargs.setdefault("default", "alpha")
    kwargs.setdefault("enabled", [])
    kwargs.setdefault("routes", {})
    kwargs.setdefault("failover", True)
    return TelephonyRouter(**kwargs)


class ParseRoutesTests(unittest.TestCase):
    def test_parses_keys_and_fallbacks(self):
        routes = parse_routes("us=twilio|signalwire; GB:TollFree=plivo, *:tollfree=telnyx, *=signalwire")
        self.assertEqual(routes["US"], ["twilio", "signalwire"])
        self.assertEqual(routes["GB:tollfree"], ["plivo"])
        self.assertEqual(routes["*:tollfree"], ["telnyx"])
        self.assertEqual(routes["*"], ["signalwire"])

    def test_empty_is_no_rules(self):
        self.assertEqual(parse_routes(""), {})

    def test_bad_rule_raises(self):
        with self.assertRaises(ValueError):
            parse_routes("twilio")
        with self.assertRaises(ValueError):
            parse_routes("US=")


class RouterTests(unittest.TestCase):
    def test_routes_enable_their_carriers(self):
        router = _router(routes={"GB": ["beta"]})
        self.assertEqual(router.enabled, ["alpha", "beta"])

    def test_most_specific_route_wins(self):
        router = _router(routes={
            "US:tollfree": ["beta"],
            "US": ["alpha"],
            "*:tollfree": ["broken"],
            "*": ["offline"],
        })
        self.assertEqual(router.route("us", "tollfree"), ["beta"])
        self.assertEqual(router.route("US", "local"), ["alpha"])
        self.assertEqual(router.route("GB", "tollfree"), ["broken"])
        self.assertEqual(router.route("GB", "local"), ["offline"])

    def test_no_rule_uses_default(self):
        self.assertEqual(_router().route("FR"), ["alpha"])

    def test_failover_appends_other_enabled_carriers(self):
        router = _router(enabled=["beta", "broken"], routes={"GB": ["broken"]})
        self.assertEqual(router.candidates("GB"), ["broken", "alpha", "beta"])
        router.failover = False
        self.assertEqual(router.candidates("GB"), ["broken"])

    def test_requested_carrier_must_be_enabled(self):
        router = _router(enabled=["beta"])
        self.assertEqual(router.candidates("US", requested="beta"), ["beta"])
        with self.assertRaises(RoutingError):
            router.candidates("US", requested="broken")

    def test_pick_skips_unconfigured(self):
        router = _router(default="offline", enabled=["beta"])
        self.assertEqual(router.pick("US").name, "beta")

    def test_provision_fails_over(self):
        router = _router(default="broken", enabled=["offline", "beta"])
        routed = asyncio.run(router.provision(country="US"))
        self.assertEqual(routed.provider.name, "beta")
        self.assertEqual([a["provider"] for a in routed.attempts], ["broken", "offline", "beta"])
        self.assertIn("no numbers", routed.attempts[0]["error"])
        self.assertEqual(routed.attempts[1]["error"], "not configured")

    def test_requested_carrier_does_not_fail_over(self):
        router = _router(enabled=["broken", "beta"])
        with self.assertRaises(RoutingError) as ctx:
            asyncio.run(router.provision(requested="broken"))
        self.assertIn("broken", str(ctx.exception))

    def test_nothing_configured(self):
        router = _router(default="offline")
        with self.assertRaises(RoutingError) as ctx:
            asyncio.run(router.provision())
        self.assertIn("No configured carrier", str(ctx.exception))

    def test_for_number_uses_stored_carrier(self):
        router = _router(enabled=["beta"])
        self.assertEqual(router.for_number({"provider": "beta"}).name, "beta")
        self.assertEqual(router.for_number({"provider": None}).name, "alpha")

    def test_describe(self):
        info = _router(default="offline", enabled=["beta"]).describe()
        self.assertEqual(info["default"], "offline")
        self.assertEqual(
            info["carriers"],
            [
                {"name": "offline", "configured": False, "default": True},
                {"name": "beta", "configured": True, "default": False},
            ],
        )


if __name__ == "__main__":
    unittest.main()
