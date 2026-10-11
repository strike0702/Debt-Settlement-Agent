"""Parse ``config/providers.yaml`` route entries into ``RouteTarget``s.

A route is an ordered list of targets the client tries in turn. An entry is a
plain ``"provider/model"`` string or a mapping with ``target`` and optional
``params`` (extra request fields), ``timeout_s`` and ``budgeted`` (counts
against the daily spend cap; see ``app.llm.client``). Unknown keys and bad
values raise ``ValueError`` at load time rather than at call time. Parsing
only: ``app.llm.client.LLMClient`` does the routing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _parse_target(spec: str) -> tuple[str, str]:
    """Split ``provider/model`` on the first slash (model may contain slashes)."""
    if "/" not in spec:
        return spec, ""
    provider, model = spec.split("/", 1)
    return provider, model


@dataclass(frozen=True)
class RouteTarget:
    """One routed target: ``provider/model``, optional request ``params`` and ``timeout_s``."""

    provider: str
    model: str
    params: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: float | None = None
    # Subject to the daily budget (see the ``app.llm.client`` module docstring).
    budgeted: bool = False

    @property
    def spec(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider


def parse_route_entry(raw: str | Mapping[str, Any]) -> RouteTarget:
    """Parse a providers.yaml route entry (plain string or ``{target, params}``)."""
    if isinstance(raw, str):
        provider, model = _parse_target(raw)
        return RouteTarget(provider, model)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("target"), str):
        raise ValueError(f"route entry needs a 'target' string: {raw!r}")
    extra = set(raw) - {"target", "params", "timeout_s", "budgeted"}
    if extra:
        raise ValueError(f"unknown route entry keys {sorted(extra)}: {raw!r}")
    params = raw.get("params") or {}
    if not isinstance(params, Mapping):
        raise ValueError(f"route params must be a mapping: {raw!r}")
    timeout_s = raw.get("timeout_s")
    if timeout_s is not None and (
        isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0
    ):
        raise ValueError(f"route timeout_s must be a positive number: {raw!r}")
    budgeted = raw.get("budgeted", False)
    if not isinstance(budgeted, bool):
        raise ValueError(f"route budgeted must be true or false: {raw!r}")
    provider, model = _parse_target(raw["target"])
    return RouteTarget(
        provider,
        model,
        dict(params),
        float(timeout_s) if timeout_s is not None else None,
        budgeted,
    )
