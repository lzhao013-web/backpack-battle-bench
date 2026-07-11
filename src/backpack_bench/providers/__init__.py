"""Provider adapter selection."""

from backpack_bench.providers.anthropic import AnthropicMessagesAdapter
from backpack_bench.providers.base import ProviderAdapter
from backpack_bench.providers.openai import OpenAIChatAdapter
from backpack_bench.schemas import ModelProfile


def adapter_for(profile: ModelProfile) -> ProviderAdapter:
    if profile.protocol == "openai_chat":
        return OpenAIChatAdapter()
    return AnthropicMessagesAdapter()


__all__ = ["ProviderAdapter", "adapter_for"]
