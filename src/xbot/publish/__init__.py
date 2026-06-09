"""Publishing backends. Selected by mode.publisher; review gating by mode.autonomous."""
from __future__ import annotations

from ..config import NS
from .dryrun import DryRunPublisher
from .publisher import Publisher


def get_publisher(cfg: NS) -> Publisher:
    backend = cfg.get("mode.publisher", "dry_run")
    if backend == "api":
        from .api_publisher import ApiPublisher
        return ApiPublisher(cfg)
    return DryRunPublisher(cfg)


__all__ = ["Publisher", "DryRunPublisher", "get_publisher"]
