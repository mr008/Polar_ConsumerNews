"""Config loader. Reads config.yaml into a dotted-access namespace.

Usage:
    cfg = load_config()
    cfg.voice.style          -> "growth_first"
    cfg.scoring_weights.likes -> 0.15
    cfg.get("thresholds.topic_fit_min", 0.5)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install the project with `pip install -e .` "
        "or run `pip install pyyaml`."
    ) from e


class NS:
    """Recursive read-only namespace over nested dicts/lists."""

    def __init__(self, data: dict):
        self._data = data or {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._data:
            raise AttributeError(f"config key '{name}' not found")
        return _wrap(self._data[name])

    def __getitem__(self, name: str) -> Any:
        return _wrap(self._data[name])

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return _wrap(node)

    def as_dict(self) -> dict:
        return dict(self._data)

    def __repr__(self) -> str:
        return f"NS({list(self._data)})"


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return NS(value)
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


def load_config(path: str | os.PathLike = "config.yaml") -> NS:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"config not found: {p.resolve()}")
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return NS(data)


def db_path(cfg: NS) -> str:
    return cfg.get("ops.db_path", "data/state.db")


def kill_switch_active(cfg: NS) -> bool:
    f = cfg.get("ops.kill_switch_file", "data/STOP")
    return Path(f).exists()
