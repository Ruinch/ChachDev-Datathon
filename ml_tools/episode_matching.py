"""Compatibility exports for the opt-in research episode-matching experiment.

The implementation lives in app.ml.episode_matching and is used by the service
factory. Importing this module does not load the dataset or call a model.
"""
from app.ml.episode_matching import (
    MATCHING_POLICY,
    admissible_trigger_ranges,
    same_episode,
)

__all__ = ['MATCHING_POLICY', 'admissible_trigger_ranges', 'same_episode']
