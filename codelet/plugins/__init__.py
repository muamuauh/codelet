"""Plugin system (P8): a first-class extension interface unifying tools, prompt
middleware, tool middleware, and system-prompt sections. See docs/plugin-architecture.md."""
from .base import Plugin, PluginContext

__all__ = ["Plugin", "PluginContext"]
