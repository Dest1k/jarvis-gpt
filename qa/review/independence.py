"""Explicit semantic-review independence labels."""

from enum import StrEnum


class IndependenceLevel(StrEnum):
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"
    SAME_MODEL_CLEAN_CONTEXT = "SAME_MODEL_CLEAN_CONTEXT"
    DIFFERENT_PROFILE = "DIFFERENT_PROFILE"
    DIFFERENT_MODEL = "DIFFERENT_MODEL"
    DIFFERENT_PROVIDER = "DIFFERENT_PROVIDER"
    HUMAN_ADJUDICATED = "HUMAN_ADJUDICATED"


def is_independent_model(level: IndependenceLevel) -> bool:
    return level in {
        IndependenceLevel.DIFFERENT_MODEL,
        IndependenceLevel.DIFFERENT_PROVIDER,
    }
