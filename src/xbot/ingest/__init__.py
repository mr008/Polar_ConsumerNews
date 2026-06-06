"""Ingestion. SourceAdapter normalizes any source into `Post` objects."""
from .adapter import SourceAdapter
from .sample_source import SampleSource

__all__ = ["SourceAdapter", "SampleSource"]
