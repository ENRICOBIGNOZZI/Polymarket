"""Causal, expanding-window PAPER evaluation for external-to-PM lead-lag."""

from .core import SAFETY, build_dataset, economic_evaluation, walk_forward

__all__ = ("SAFETY", "build_dataset", "economic_evaluation", "walk_forward")
