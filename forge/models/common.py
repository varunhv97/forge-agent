from __future__ import annotations

from enum import Enum
from typing import Literal, TypeAlias

# Forge's own effort-level type — structurally identical to the Claude Agent
# SDK's `claude_agent_sdk.types.EffortLevel`, but defined here so that
# forge/models/* and forge/controller/* never import the SDK package at all
# (only forge/adapters/execution/* does). The SDK adapter is the one place
# that translates a value of this type into whatever the real SDK expects.
EffortLevel: TypeAlias = Literal["low", "medium", "high", "xhigh", "max"]


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
