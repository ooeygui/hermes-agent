"""Microsoft Foundry Local provider profile.

Foundry Local (https://learn.microsoft.com/azure/ai-foundry/foundry-local/) is
Microsoft's on-device inference runtime. Hermes integrates with it through
the **native** ``foundry_local_sdk`` Python package — there is no localhost
HTTP hop. ``run_agent._create_openai_client`` and the auxiliary client both
dispatch on ``provider == "foundry-local"`` to construct
``agent.foundry_local_adapter.FoundryLocalClient`` instead of an OpenAI HTTP
client.

The profile still declares ``auth_type="api_key"`` (with no key required at
run time) so the standard credential plumbing — ``hermes setup``, the
model picker, the ``--provider`` flag, ``hermes doctor`` — wires up
automatically. ``fetch_models`` enumerates the local catalog via the SDK so
the model picker shows what is actually downloadable / loaded on this
machine.
"""

from __future__ import annotations

import logging
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


class FoundryLocalProfile(ProviderProfile):
    """Foundry Local — native, in-process inference (no HTTP)."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Enumerate the local Foundry catalog via the native SDK.

        Returns the list of model aliases known to Foundry Local on this
        machine. Returns ``None`` (so callers fall back to ``fallback_models``)
        when the SDK is not installed or the runtime cannot be reached.
        """
        try:
            from agent.foundry_local_adapter import _get_manager
        except Exception:  # pragma: no cover - import guard
            return None
        try:
            manager = _get_manager()
            models = manager.catalog.list_models()
            aliases = sorted({m.alias for m in models if getattr(m, "alias", None)})
            return aliases or None
        except Exception as exc:
            logger.debug("Foundry Local: catalog enumeration failed: %s", exc)
            return None


foundry_local = FoundryLocalProfile(
    name="foundry-local",
    aliases=("foundry", "foundrylocal"),
    display_name="Foundry Local",
    description="Microsoft Foundry Local — on-device native inference (no HTTP)",
    signup_url="https://learn.microsoft.com/azure/ai-foundry/foundry-local/",
    # No env vars: the native SDK is in-process, no API key, no base URL.
    env_vars=(),
    # Sentinel base_url used purely for plumbing (logging / client metadata).
    # The adapter never makes HTTP requests against this value.
    base_url="foundry-local://",
    auth_type="api_key",
    # Curated fallback list — common Foundry Local catalog aliases shown
    # when SDK enumeration is unavailable (e.g. SDK not installed yet).
    fallback_models=(
        "qwen2.5-0.5b",
        "phi-4-mini",
        "qwen2.5-7b-instruct",
        "phi-3.5-mini-instruct",
        "deepseek-r1-distill-qwen-7b",
        "mistral-7b-instruct-v0.2",
    ),
)

register_provider(foundry_local)
