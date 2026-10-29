"""Host-model task learning and evidence-bound knowledge feedback.

Imports are lazy: importing the SDK never creates a database, starts a server,
or resolves credentials. The host distills its own chat before submitting it.
"""

from __future__ import annotations

from pathlib import Path


def _require_agreement(action: str, state_dir: str | Path | None = None) -> None:
    from ...knowledge_service.agreement import AgreementStore

    AgreementStore.from_env(state_dir=state_dir).require(action)


class KnowledgeAgreementClient:
    """Inspect or revoke the human agreement; acceptance is never an SDK action."""

    def __init__(self, *, state_dir: str | Path | None = None):
        self._state_dir = state_dir

    def _store(self):
        from ...knowledge_service.agreement import AgreementStore

        return AgreementStore.from_env(state_dir=self._state_dir)

    def document(self) -> dict:
        return self._store().document()

    def status(self) -> dict:
        return self._store().status()

    def revoke(self) -> dict:
        return self._store().revoke()


class KnowledgeLearningClient:
    """Use the same task-completion service as MCP and the learning CLI."""

    def __init__(self, *, state_dir: str | Path | None = None,
                 workspace_root: str | Path | None = None):
        self._state_dir = state_dir
        self._workspace_root = workspace_root
        self._service = None

    def _runtime(self):
        if self._service is None:
            from ...knowledge_service.learning import TaskLearningService

            self._service = (TaskLearningService(self._state_dir, self._workspace_root)
                             if self._state_dir is not None else
                             TaskLearningService.from_env(workspace_root=self._workspace_root))
        return self._service

    def prepare(self, *, task_id: str, repository: str, source_pin: str,
                lessons: list[dict]) -> dict:
        _require_agreement("knowledge_learning.prepare", self._state_dir)
        return self._runtime().prepare(task_id, repository, source_pin, lessons)

    def submit(self, *, task_id: str, packet_digest: str, proposals: dict) -> dict:
        _require_agreement("knowledge_learning.submit", self._state_dir)
        return self._runtime().submit(task_id, packet_digest, proposals)

    def status(self, task_id: str) -> dict:
        return self._runtime().status(task_id)


class KnowledgeFeedbackClient:
    """Grade selected, actually used targets from provider-issued deliveries."""

    def __init__(self, *, state_dir: str | Path | None = None):
        self._state_dir = state_dir

    def _store(self):
        from ...knowledge_service.feedback import KnowledgeFeedbackStore

        return (KnowledgeFeedbackStore.from_env(state_dir=self._state_dir) if self._state_dir is not None
                else KnowledgeFeedbackStore.from_env())

    def record(self, *, task_id: str, delivery_id: str, ratings: list[dict]) -> dict:
        _require_agreement("knowledge_feedback.record", self._state_dir)
        return self._store().record(task_id=task_id, delivery_id=delivery_id, ratings=ratings)

    def report(self, *, repository: str = "", limit: int = 100,
               scope: str = "auto") -> dict:
        """Read the configured team aggregate, or explicitly select local data.

        Auto selects central when configured. A central failure is an error;
        it never presents a partial local ledger as the team aggregate.
        """
        return self._store().report_for_scope(repository=repository, limit=limit, scope=scope)

    def sync(self, *, limit: int = 100) -> dict:
        """Retry a bounded batch of durable feedback uploads."""
        _require_agreement("knowledge_feedback.sync", self._state_dir)
        return self._store().sync_pending(limit=limit)

    def sync_status(self) -> dict:
        """Inspect upload state without making a network request."""
        return self._store().sync_status()
