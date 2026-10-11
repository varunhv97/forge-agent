from __future__ import annotations

from enum import Enum


class NodeKind(str, Enum):
    EXECUTION = "execution"
    DECISION = "decision"
    MECHANICAL = "mechanical"  # subtype of execution with no LLM call


class Mode(str, Enum):
    FEATURE_BLAST = "feature_blast"
    # more added here as each future mode (plan §2.2) actually gets built
