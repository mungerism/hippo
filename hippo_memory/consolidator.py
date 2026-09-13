"""Cold Path orchestration for memory consolidation (Issue #25).

Wires the four stable seams into one auditable run and exposes the public
entry point future schedulers may call — without ever being attached to
the foreground ``add_memory`` path:

    Candidate Discovery (#21) -> Relationship Classification + Winner
    Arbitration (#22) -> stable Operation Plan + Idempotent Apply (#23)
    -> Lifecycle Invariant (#24, recall side) -> aggregated
    ``ConsolidationResult``.

Cold Path classifier isolation: the classifier's LLM is an independent
instance (same provider/model/credentials, deterministic ``temperature=0``)
built from a deep copy of the provider config. The Hot/Warm shared
``engine.memory.llm`` is never handed to the classifier and never mutated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import copy
import logging
import re
from typing import Any, Dict, List, Optional

from hippo_memory.apply import (
    OperationJournal,
    OperationPlan,
    RESULT_APPLIED,
    RESULT_ALREADY_APPLIED,
    RESULT_FAILED,
    RESULT_STALE_PLAN,
    ConsolidationApplier,
    build_operation_plan,
)
from hippo_memory.candidate_discovery import (
    CandidateDiscovery,
    CandidateScanLimitExceeded,
    resolve_scope_identity,
)
from hippo_memory.lifecycle import is_active_memory
from hippo_memory.decision import (
    RELATION_CONFLICT,
    RELATION_DISTINCT,
    RELATION_EQUIVALENT,
    ConsolidationDecider,
    ConsolidationDecision,
    RelationshipClassifier,
    WinnerArbiter,
)

logger = logging.getLogger(__name__)


@dataclass
class ConsolidationResult:
    """Aggregated, auditable outcome of one consolidation run."""

    scope: str
    project_id: Optional[str] = None
    scanned: int = 0
    seeds: int = 0
    candidate_pairs: int = 0
    classified_equivalent: int = 0
    classified_conflict: int = 0
    classified_distinct: int = 0
    merged: int = 0
    superseded: int = 0
    unchanged: int = 0
    stale_plans: int = 0
    errors: List[str] = field(default_factory=list)
    details: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return not self.errors

    def stats(self) -> Dict[str, int]:
        """Counter-only view for CLI/reporting, in canonical order."""
        return {
            "scanned": self.scanned,
            "seeds": self.seeds,
            "candidate_pairs": self.candidate_pairs,
            "classified_equivalent": self.classified_equivalent,
            "classified_conflict": self.classified_conflict,
            "classified_distinct": self.classified_distinct,
            "merged": self.merged,
            "superseded": self.superseded,
            "unchanged": self.unchanged,
            "stale_plans": self.stale_plans,
            "errors": len(self.errors),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "project_id": self.project_id,
            "scanned": self.scanned,
            "seeds": self.seeds,
            "candidate_pairs": self.candidate_pairs,
            "classified_equivalent": self.classified_equivalent,
            "classified_conflict": self.classified_conflict,
            "classified_distinct": self.classified_distinct,
            "merged": self.merged,
            "superseded": self.superseded,
            "unchanged": self.unchanged,
            "stale_plans": self.stale_plans,
            "errors": list(self.errors),
            "details": [dict(detail) for detail in self.details],
        }


def _build_isolated_classifier_llm(engine: Any) -> Optional[Any]:
    """Independent LLM instance for the Cold Path classifier.

    Shares provider/model/credentials with the Hot/Warm path but NEVER the
    mutable config object: the provider config is deep-copied and sampling
    is pinned to ``temperature=0``. The shared ``engine.memory.llm`` is not
    read after this point and never mutated. Returns None when no provider
    can be resolved, in which case the classifier runs in deterministic
    exact-match-only mode (fail-closed to DISTINCT).
    """
    try:
        llm_section = (engine.config.get_mem0_config() or {}).get("llm") or {}
        provider = llm_section.get("provider")
        if not provider:
            return None
        llm_config = copy.deepcopy(llm_section.get("config") or {})
        llm_config["temperature"] = 0
        from mem0.utils.factory import LlmFactory

        return LlmFactory.create(provider, llm_config)
    except Exception as exc:
        # Cold Path must never break because the classifier LLM is
        # unavailable: degrade to deterministic-only classification.
        logger.warning("Cold Path classifier LLM unavailable (%s); "
                       "falling back to exact-match-only classification", exc)
        return None


def parse_since(value: Optional[str]) -> Optional[datetime]:
    """Parse a CLI ``--since`` value: ``<n>h/d/w`` relative window or an
    ISO-8601 datetime. None/empty means no incremental window."""
    if not value:
        return None
    text = value.strip()
    match = re.fullmatch(r"(\d+)\s*([hdw])", text, re.IGNORECASE)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        deltas = {"h": timedelta(hours=amount), "d": timedelta(days=amount),
                  "w": timedelta(weeks=amount)}
        now = datetime.now(timezone.utc)
        return now - deltas[unit]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"invalid --since value {value!r}: use <n>h / <n>d / <n>w "
            "or an ISO-8601 datetime"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now(timezone.utc).tzinfo)
    return parsed


class MemoryConsolidator:
    """Orchestrates the full Cold Path over the four stable seams."""

    def __init__(
        self,
        engine: Any,
        *,
        classifier: Optional[Any] = None,
        discovery: Optional[CandidateDiscovery] = None,
        applier: Optional[ConsolidationApplier] = None,
        classifier_llm: Optional[Any] = None,
    ) -> None:
        self.engine = engine
        self._classifier_override = classifier
        self._classifier_llm = classifier_llm
        self.discovery = discovery or CandidateDiscovery(engine)
        self._classifier_degraded = False
        self.applier = applier or ConsolidationApplier(
            engine,
            journal=OperationJournal(
                getattr(
                    getattr(engine, "config", None),
                    "consolidation_operations_dir",
                    None,
                )
            ),
        )
        self._decider: Optional[ConsolidationDecider] = None

    def _resolve_decider(self) -> ConsolidationDecider:
        """Build the decision layer lazily; injected classifiers win."""
        if self._decider is None:
            self._classifier_degraded = False
            if self._classifier_override is not None:
                classifier = self._classifier_override
            else:
                llm = self._classifier_llm
                if llm is None:
                    llm = _build_isolated_classifier_llm(self.engine)
                classifier = RelationshipClassifier(llm=llm)
                self._classifier_degraded = llm is None
            self._decider = ConsolidationDecider(
                classifier=classifier,
                arbiter=WinnerArbiter(),
            )
        return self._decider

    def consolidate(
        self,
        scope: str = "project",
        project_id: Optional[str] = None,
        since: Optional[datetime] = None,
        dry_run: bool = False,
        *,
        user_id: Optional[str] = None,
    ) -> ConsolidationResult:
        """Run one full Cold Path pass over an identity scope.

        Counter semantics: ``unchanged`` counts idempotent replays only
        (``already_applied``); DISTINCT pairs are reported solely through
        ``classified_distinct``. ``dry_run`` records every decision in
        ``details`` with ``result="dry_run"`` and performs no mutation.
        """
        result = ConsolidationResult(scope=scope, project_id=project_id)

        try:
            identity = resolve_scope_identity(
                self.engine, scope, project_id=project_id, user_id=user_id
            )
        except ValueError as exc:
            result.errors.append(f"[discovery] {exc}")
            return result

        # Crash recovery BEFORE discovery: a crash in the window after the
        # loser was superseded but before the journal was completed leaves
        # an unfinished operation the (now superseded) pair can no longer
        # be rediscovered for — finish it here instead (#25 review round 3).
        self._recover_unfinished(identity, result)

        try:
            candidate_set = self.discovery.discover(
                scope=scope, project_id=project_id, since=since, user_id=user_id
            )
        except CandidateScanLimitExceeded as exc:
            result.errors.append(f"[discovery] candidate discovery failed: {exc}")
            return result
        result.scanned = candidate_set.scanned
        result.seeds = candidate_set.seeds
        result.candidate_pairs = len(candidate_set.candidate_pairs)

        decider = self._resolve_decider()
        if self._classifier_degraded:
            # Fail-closed deterministic mode is safe but means provider
            # misconfiguration would silently stop conflict detection —
            # surface it loudly instead of reporting a clean run.
            result.errors.append(
                "[classifier] isolated Cold Path LLM unavailable; "
                "running deterministic exact-match-only mode "
                "(non-exact pairs fail closed to DISTINCT)"
            )

        for edge in candidate_set.candidate_pairs:
            # Phase/operation context for the failure-report contract.
            context: Dict[str, Any] = {"phase": "decision", "operation_id": None}
            try:
                self._process_edge(edge, decider, dry_run, result, context)
            except Exception as exc:  # one failure must not sink the batch
                logger.warning("consolidation of edge %s failed: %s", edge, exc)
                operation = (
                    f" operation {context['operation_id']}"
                    if context["operation_id"]
                    else ""
                )
                result.errors.append(
                    f"[{context['phase']}] edge {edge.seed_id}/{edge.neighbor_id}"
                    f"{operation}: {exc}"
                )
        return result

    def _recover_unfinished(self, identity, result: ConsolidationResult) -> None:
        """Resume this identity's unfinished operations before discovery."""
        for entry in self.applier.journal.unfinished():
            try:
                if list(entry.get("identity") or []) != [identity[0], identity[1]]:
                    continue
                plan = OperationPlan(
                    operation_id=str(entry["operation_id"]),
                    user_id=identity[0],
                    agent_id=identity[1],
                    relation=str(entry["relation"]),
                    winner_id=str(entry["winner_id"]),
                    loser_id=str(entry["loser_id"]),
                    observed_winner_version=str(entry["observed_winner_version"]),
                    observed_loser_version=str(entry["observed_loser_version"]),
                    reason=str(entry.get("reason") or ""),
                )
            except (KeyError, TypeError, ValueError) as exc:
                result.errors.append(f"[recovery] malformed journal entry: {exc}")
                continue
            try:
                apply_result = self.applier.apply(plan)
            except Exception as exc:
                result.errors.append(
                    f"[recovery] operation {plan.operation_id}: {exc}"
                )
                continue
            if apply_result.status == RESULT_FAILED:
                result.errors.append(
                    f"[recovery] operation {plan.operation_id} failed: "
                    f"{apply_result.error}"
                )
            elif apply_result.status == RESULT_STALE_PLAN:
                result.stale_plans += 1
            elif apply_result.status == RESULT_APPLIED:
                result.merged += 1
                if apply_result.loser_superseded:
                    result.superseded += 1
            result.details.append(
                {
                    "operation_id": plan.operation_id,
                    "relation": plan.relation,
                    "winner_id": plan.winner_id,
                    "loser_id": plan.loser_id,
                    "reason": plan.reason,
                    "result": f"recovery:{apply_result.status}",
                }
            )

    def _process_edge(
        self, edge, decider, dry_run: bool, result: ConsolidationResult, context: Dict[str, Any]
    ) -> None:
        first = self.engine.get(edge.seed_id)
        second = self.engine.get(edge.neighbor_id)
        if first is None or second is None:
            result.errors.append(
                f"[decision] edge {edge.seed_id}/{edge.neighbor_id}: "
                "record vanished since discovery"
            )
            return

        # Edges were all discovered up front: an earlier edge in this batch
        # may already have superseded one side (e.g. A-B then B-C). Re-check
        # the lifecycle invariant so superseded records never take part in
        # another winner/loser decision.
        if not is_active_memory(first) or not is_active_memory(second):
            result.details.append(
                {
                    "operation_id": None,
                    "relation": None,
                    "winner_id": None,
                    "loser_id": None,
                    "reason": "side already superseded by an earlier operation",
                    "result": "skipped_superseded",
                }
            )
            return

        decision = decider.decide(first, second)
        if decision.relation == RELATION_DISTINCT:
            result.classified_distinct += 1
            result.details.append(self._detail(decision, result="distinct"))
            return
        if decision.relation == RELATION_EQUIVALENT:
            result.classified_equivalent += 1
        elif decision.relation == RELATION_CONFLICT:
            result.classified_conflict += 1
        else:
            raise ValueError(f"unknown relation from decision layer: {decision.relation!r}")

        if decision.winner_id not in (edge.seed_id, edge.neighbor_id) or (
            decision.loser_id not in (edge.seed_id, edge.neighbor_id)
        ):
            raise ValueError(
                f"decision winner/loser {decision.winner_id}/{decision.loser_id} "
                f"does not match discovered edge {edge.seed_id}/{edge.neighbor_id}"
            )

        winner_record, loser_record = (
            (first, second)
            if decision.winner_id == edge.seed_id
            else (second, first)
        )
        context["phase"] = "planning"
        plan = build_operation_plan(
            decision, winner_record=winner_record, loser_record=loser_record
        )
        context["operation_id"] = plan.operation_id

        if dry_run:
            # Preview only: no mutation, no journal transition.
            result.details.append(self._detail(decision, result="dry_run", plan=plan))
            return

        context["phase"] = "apply"
        try:
            apply_result = self.applier.apply(plan)
        except Exception as exc:
            # A crash inside apply (store down, simulated fault) is reported
            # with its phase; the journal marks the operation resumable.
            result.errors.append(
                f"[apply] edge {edge.seed_id}/{edge.neighbor_id}: {exc}"
            )
            return
        detail = self._detail(decision, result=apply_result.status, plan=plan)
        detail["apply_error"] = apply_result.error
        result.details.append(detail)

        if apply_result.status == RESULT_APPLIED:
            if decision.relation == RELATION_EQUIVALENT:
                result.merged += 1
            if apply_result.loser_superseded:
                result.superseded += 1
        elif apply_result.status == RESULT_ALREADY_APPLIED:
            result.unchanged += 1
        elif apply_result.status == RESULT_STALE_PLAN:
            result.stale_plans += 1
        elif apply_result.status == RESULT_FAILED:
            result.errors.append(
                f"[apply] operation {plan.operation_id} failed: {apply_result.error}"
            )

    @staticmethod
    def _detail(
        decision: ConsolidationDecision,
        *,
        result: str,
        plan: Optional[Any] = None,
    ) -> Dict[str, Any]:
        detail = {
            "operation_id": getattr(plan, "operation_id", None),
            "relation": decision.relation,
            "winner_id": decision.winner_id,
            "loser_id": decision.loser_id,
            "reason": decision.reason,
            "result": result,
        }
        return detail
