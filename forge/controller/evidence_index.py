from __future__ import annotations

from forge.models.evidence import Evidence


class EvidenceIndex:
    """
    In-memory evidence lookup. Backed by SQLite from M1 onward; for M0 this
    is just a plain wrapper so the validator has one stable interface to
    depend on regardless of what's behind it later.
    """

    def __init__(self, evidence: list[Evidence] | None = None) -> None:
        self._by_id: dict[str, Evidence] = {}
        self._by_task_id: dict[str, list[Evidence]] = {}
        for e in evidence or []:
            self.add(e)

    def add(self, evidence: Evidence) -> None:
        self._by_id[evidence.id] = evidence
        self._by_task_id.setdefault(evidence.task_id, []).append(evidence)

    def get(self, evidence_id: str) -> Evidence | None:
        return self._by_id.get(evidence_id)

    def for_task(self, task_id: str) -> list[Evidence]:
        return list(self._by_task_id.get(task_id, []))
