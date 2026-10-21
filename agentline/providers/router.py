"""Route numbers to carriers when more than one is enabled.

A deployment can run SignalWire, Twilio, Plivo, Telnyx, and custom carriers
side by side. Each number remembers the carrier that sold it, so calls, SMS,
and hangups always go back through that carrier. The router only decides
which carrier sells or attaches a new number:

Enabled carriers are ``TELEPHONY_PROVIDER``, ``TELEPHONY_PROVIDERS``, and any
carrier a route names. When ``TELEPHONY_PROVIDERS`` is empty, every built-in
carrier with credentials in the environment is enabled too.

For each new number the router tries, in order:

1. The ``provider`` the API caller asked for.
2. The first ``TELEPHONY_ROUTES`` rule that matches the country and type.
3. ``TELEPHONY_PROVIDER``.

With ``TELEPHONY_FAILOVER`` on, a failed purchase moves to the next carrier
in the rule, then to every other enabled carrier that has credentials.

``TELEPHONY_ROUTES`` example::

    US=twilio|signalwire, CA=twilio, GB=plivo, *:tollfree=telnyx, *=signalwire

Keys are ``COUNTRY``, ``COUNTRY:type``, ``*:type``, or ``*``. The most
specific key wins. ``|`` lists fallbacks for that key.
"""

import logging
from dataclasses import dataclass

from agentline.config import settings
from agentline.providers.base import ProvisionedNumber
from agentline.providers.registry import BUILTIN_TELEPHONY, get_provider

logger = logging.getLogger(__name__)


class RoutingError(RuntimeError):
    """No enabled carrier could take the request."""


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def parse_routes(spec: str) -> dict[str, list[str]]:
    """Parse ``TELEPHONY_ROUTES`` into ``{"US": ["twilio", "signalwire"], ...}``."""
    routes: dict[str, list[str]] = {}
    for rule in _split(spec):
        if "=" not in rule:
            raise ValueError(f"Bad TELEPHONY_ROUTES rule '{rule}'. Use KEY=carrier.")
        key, target = rule.split("=", 1)
        key = key.strip()
        if ":" in key:
            country, kind = key.split(":", 1)
            key = f"{country.strip().upper() or '*'}:{kind.strip().lower()}"
        else:
            key = key.upper() or "*"
        carriers = [name.strip() for name in target.split("|") if name.strip()]
        if not carriers:
            raise ValueError(f"TELEPHONY_ROUTES rule '{rule}' names no carrier.")
        routes[key] = carriers
    return routes


@dataclass
class RoutedNumber:
    provider: object
    number: ProvisionedNumber
    attempts: list[dict]


class TelephonyRouter:
    def __init__(
        self,
        default: str | None = None,
        enabled: list[str] | None = None,
        routes: dict[str, list[str]] | None = None,
        failover: bool | None = None,
    ):
        self.default = (default or settings.TELEPHONY_PROVIDER or "signalwire").strip()
        if enabled is not None:
            names = enabled
        else:
            # An empty TELEPHONY_PROVIDERS enables every built-in with credentials.
            names = _split(settings.TELEPHONY_PROVIDERS) or [
                name for name in BUILTIN_TELEPHONY if get_provider(name).is_configured()
            ]
        self.routes = routes if routes is not None else parse_routes(settings.TELEPHONY_ROUTES)
        self.failover = settings.TELEPHONY_FAILOVER if failover is None else failover

        # Every carrier a rule or the default names is enabled implicitly.
        ordered = [self.default, *names]
        for chain in self.routes.values():
            ordered.extend(chain)
        self.enabled: list[str] = list(dict.fromkeys(ordered))

    def is_enabled(self, name: str) -> bool:
        return name in self.enabled

    def route(self, country: str = "US", number_type: str = "local") -> list[str]:
        """Carriers a rule picks for this country and type, best first."""
        iso = (country or "US").upper()
        kind = (number_type or "local").lower()
        for key in (f"{iso}:{kind}", iso, f"*:{kind}", "*"):
            if key in self.routes:
                return list(self.routes[key])
        return [self.default]

    def candidates(
        self,
        country: str = "US",
        number_type: str = "local",
        requested: str | None = None,
    ) -> list[str]:
        """Carriers to try in order. An explicit request is never swapped out."""
        if requested:
            if not self.is_enabled(requested):
                raise RoutingError(
                    f"Carrier '{requested}' is not enabled. "
                    f"Enabled carriers: {', '.join(self.enabled)}. "
                    "Add it to TELEPHONY_PROVIDERS."
                )
            return [requested]
        chain = self.route(country, number_type)
        if self.failover:
            chain += [name for name in self.enabled if name not in chain]
        return list(dict.fromkeys(chain))

    def pick(
        self,
        country: str = "US",
        number_type: str = "local",
        requested: str | None = None,
    ):
        """First configured carrier for this request, without buying anything."""
        for name in self.candidates(country, number_type, requested):
            provider = get_provider(name)
            if provider.is_configured():
                return provider
        raise RoutingError(self._none_configured(country, number_type, requested))

    async def provision(
        self,
        country: str = "US",
        number_type: str = "local",
        area_code: str | None = None,
        pattern: str | None = None,
        agent_id: str | None = None,
        requested: str | None = None,
    ) -> RoutedNumber:
        """Buy a number from the first carrier in the route that succeeds."""
        attempts: list[dict] = []
        for name in self.candidates(country, number_type, requested):
            provider = get_provider(name)
            if not provider.is_configured():
                attempts.append({"provider": name, "error": "not configured"})
                continue
            try:
                number = await provider.provision_number(
                    country=country,
                    number_type=number_type,
                    area_code=area_code,
                    pattern=pattern,
                    agent_id=agent_id,
                )
            except Exception as exc:
                logger.warning("Provisioning on %s failed: %s", name, exc)
                attempts.append({"provider": name, "error": str(exc)})
                continue
            attempts.append({"provider": name, "error": None})
            return RoutedNumber(provider=provider, number=number, attempts=attempts)

        if not any(a["error"] != "not configured" for a in attempts):
            raise RoutingError(self._none_configured(country, number_type, requested))
        summary = "; ".join(f"{a['provider']}: {a['error']}" for a in attempts)
        raise RoutingError(f"Every carrier failed to provision a number. {summary}")

    def for_number(self, number) -> object:
        """Carrier that owns a stored number row. Old rows fall back to the default."""
        name = None
        if number is not None:
            name = number.get("provider") if hasattr(number, "get") else number["provider"]
        return get_provider(name or self.default)

    async def reconfigure_active_numbers(self) -> None:
        """Point every enabled carrier's numbers at this server."""
        for name in self.enabled:
            try:
                provider = get_provider(name)
            except Exception as exc:
                logger.warning("Skipping carrier %s: %s", name, exc)
                continue
            if not provider.is_configured():
                logger.info("Skipping number webhook refresh — %s is not configured.", name)
                continue
            try:
                await provider.reconfigure_active_numbers()
            except Exception as exc:
                logger.warning("Webhook refresh failed for carrier %s: %s", name, exc)

    def describe(self) -> dict:
        carriers = []
        for name in self.enabled:
            try:
                configured = get_provider(name).is_configured()
            except Exception:
                configured = False
            carriers.append({"name": name, "configured": configured, "default": name == self.default})
        return {
            "default": self.default,
            "failover": self.failover,
            "routes": self.routes,
            "carriers": carriers,
        }

    def _none_configured(self, country: str, number_type: str, requested: str | None) -> str:
        tried = ", ".join(self.candidates(country, number_type, requested))
        return (
            f"No configured carrier for {country} {number_type} numbers (tried {tried}). "
            "Set that carrier's credentials in the environment."
        )


def get_router() -> TelephonyRouter:
    """Build a router from the current settings."""
    return TelephonyRouter()
