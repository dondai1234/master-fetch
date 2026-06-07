"""Hound — Web research for AI agents.

$0 forever. Fetch any page with anti-bot bypass plus web search.
"""

__version__ = "3.2.0"

# Lazy imports — server pulls in heavy deps (scrapling, playwright, etc.)
# Other modules (cache, security, domain_intel) are lightweight and
# can be imported directly for testing without full dependency chain.


def __getattr__(name: str):
    """Lazy attribute access for server-level exports."""
    _lazy_exports = {
        "MasterFetchServer",
        "ResponseModel",
        "BulkResponseModel",
        "ArticleModel",
        "SessionInfo",
        "SessionCreatedModel",
        "SessionClosedModel",
        "CacheInfoModel",
        "main",
    }
    if name in _lazy_exports:
        from master_fetch.server import (  # noqa: E402
            MasterFetchServer,
            ResponseModel,
            BulkResponseModel,
            ArticleModel,
            SessionInfo,
            SessionCreatedModel,
            SessionClosedModel,
            CacheInfoModel,
            main,
        )
        return locals()[name]
    raise AttributeError(f"module 'master_fetch' has no attribute '{name}'")


__all__ = [
    "MasterFetchServer",
    "ResponseModel",
    "BulkResponseModel",
    "ArticleModel",
    "SessionInfo",
    "SessionCreatedModel",
    "SessionClosedModel",
    "CacheInfoModel",
    "main",
]
