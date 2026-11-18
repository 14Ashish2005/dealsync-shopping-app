from __future__ import annotations

from collections.abc import Iterable

from backend.providers.base import ProviderAdapter, ProviderUnavailableError
from backend.providers.demo import DemoProviderAdapter
from backend.providers.local_catalog import LocalCatalogAdapter


class ProviderRegistry:
    def __init__(self, providers: Iterable[ProviderAdapter] = ()) -> None:
        self._providers: dict[str, ProviderAdapter] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: ProviderAdapter) -> None:
        if not isinstance(provider, ProviderAdapter):
            raise TypeError("Registered providers must implement ProviderAdapter.")

        key = provider.provider_key
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Provider key must be non-empty text.")
        if key in self._providers:
            raise ValueError(f"Provider key {key!r} is already registered.")
        self._providers[key] = provider

    def get(self, provider_key: str) -> ProviderAdapter:
        try:
            return self._providers[provider_key]
        except KeyError as exc:
            raise ProviderUnavailableError(
                provider_key, "The provider is not registered."
            ) from exc

    @property
    def provider_keys(self) -> tuple[str, ...]:
        return tuple(self._providers)


def create_default_registry(
    local_adapter: LocalCatalogAdapter | None = None,
) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(local_adapter or LocalCatalogAdapter())
    registry.register(DemoProviderAdapter())
    return registry


DEFAULT_PROVIDER_REGISTRY = create_default_registry()
