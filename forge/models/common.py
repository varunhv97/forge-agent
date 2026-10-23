from __future__ import annotations

from enum import Enum


class NodeKind(str, Enum):
    EXECUTION = "execution"
    DECISION = "decision"
    MECHANICAL = "mechanical"  # subtype of execution with no LLM call


class Mode(str, Enum):
    FEATURE_BLAST = "feature_blast"
    CUSTOM = "custom"
    # New modes are added here only once they're actually designed and
    # built — this enum should never list a mode with no template behind
    # it. CUSTOM covers every user-authored custom DAG template (loaded by
    # forge/modes/custom_dag.py from .forge/dags/*.json): one mode value,
    # not one per template — Request.template_name is what names which
    # template produced a given request.
