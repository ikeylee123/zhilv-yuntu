"""Bounded selection orchestration around the frozen Phase 3B core."""
import copy
import hashlib
import logging
import math
import time
from uuid import uuid4
from pydantic import ValidationError
from app.agents.candidate_selector import (CandidateSelector, CANDIDATE_SELECTOR_PROMPT_VERSION,
    SELECTOR_POLICY_VERSION)
from app.models.candidate_selection import (ReplanSelectionRequest, SelectionDecision,
    SelectionMetadata, CandidateSelectionInput, TargetContext, HardConstraintSummary, SelectionCandidate)
from app.models.replanning import CandidatePreparationRequest, StateError
from app.services.replanning_candidates import authorized_candidate
from app.services.replanning_scope import authorize_scope, precheck_constraints, matches

logger = logging.getLogger(__name__)
_AUTH_ERRORS = {"CANDIDATE_SET_NOT_FOUND", "CANDIDATE_NOT_AUTHORIZED", "CANDIDATE_SET_EXPIRED"}


def _text(value):
    return str(value)[:500] if value else None


def selection_input(state, request, grant, selection_id):
    day = next(d for d in state.itinerary.days if d.day_index == request.target_day_index)
    target = next((s for s in day.spots if s.activity_id == request.target_activity_id), None)
    relevant = [ref if isinstance(ref, str) else ref.name or ref.entity_id
        for ref in state.itinerary.constraints.must_visit or []
        if target is not None and matches(ref, target)]
    preferences = state.itinerary.constraints.soft_preferences
    if preferences is None:
        preferences = state.itinerary.original_request.preferences
    pace = state.itinerary.original_request.pace
    return CandidateSelectionInput(selection_id=selection_id, operation=request.change_type.value,
        target_context=TargetContext(name=_text(target.name) if target else None,
            day_theme=_text(day.theme),
            authorized_start_time=target.start_time if target else request.start_time,
            authorized_end_time=target.end_time if target else request.end_time),
        hard_constraint_summary=HardConstraintSummary(relevant_must_visit=tuple(_text(v) for v in relevant[:20])),
        soft_preferences=tuple(_text(v) for v in [*(preferences or []), *([pace] if pace else [])][:20] if v),
        candidate_set_id=grant.candidate_set_id,
        candidates=tuple(SelectionCandidate(candidate_id=c.poi.poi_id, name=_text(c.poi.name),
            category="spot", address_or_area=_text(c.poi.address), provider_type=_text(c.poi.provider_type))
            for c in grant.candidates))


class BoundedSelectionService:
    def __init__(self, core, selector=None, *, deadline_seconds=None, monotonic=time.monotonic):
        self.core, self.selector = core, selector
        self.monotonic = monotonic
        from app import config
        self.deadline_seconds = deadline_seconds if deadline_seconds is not None else config.REPLAN_SELECTION_DEADLINE_SECONDS

    def replan(self, request):
        request = ReplanSelectionRequest.model_validate(request.model_dump())
        # No selector construction, provider access or added deadlines on explicit/remove/time.
        if request.selection_mode == "explicit" or request.change_type in ("remove", "modify_time"):
            result = self.core.replan(request.explicit_request())
            return SelectionDecision(**result.model_dump())
        scope = metadata = None
        started = self.monotonic()
        def check_deadline():
            if self.monotonic()-started >= self.deadline_seconds:
                raise StateError("LLM_SELECTION_TIMEOUT")
        def finish(result):
            if metadata is not None:
                logger.info("candidate_selection %s", {
                    "selection_id": metadata.selection_id,
                    "trip_ref": hashlib.sha256(request.trip_id.encode()).hexdigest()[:16],
                    "base_version": request.base_plan_version,
                    "decision": result.decision, "reason": result.reason,
                    **metadata.model_dump(mode="json", exclude={"rationale", "selection_id"})})
            return SelectionDecision(**result.model_dump(), selection_metadata=metadata)
        try:
            state = self.core.loader(request.trip_id)
            scope = authorize_scope(state, request)
            precheck_constraints(state, request)
            try:
                selector = self.selector or CandidateSelector()
            except ValueError as exc:
                raise StateError("LLM_SELECTION_PROVIDER_ERROR") from exc
            if not (math.isfinite(self.deadline_seconds) and 0 < selector.timeout_seconds < self.deadline_seconds):
                raise StateError("LLM_SELECTION_PROVIDER_ERROR")
            check_deadline()
            try:
                grant = self.core.prepare(CandidatePreparationRequest(
                    **{k: v for k, v in request.model_dump().items() if k in CandidatePreparationRequest.model_fields}))
            except StateError:
                raise
            except Exception as exc:
                raise StateError("REPLAN_UNVERIFIABLE") from exc
            check_deadline()
            state = self.core.loader(request.trip_id)
            authorize_scope(state, request)
            if not grant.candidates:
                raise StateError("LLM_SELECTION_EMPTY")
            # Check every candidate's current grant before exposing any metadata to the model.
            if self.core.store.get(grant.candidate_set_id) != grant:
                raise StateError("LLM_SELECTION_NOT_AUTHORIZED")
            for candidate in grant.candidates:
                explicit = request.explicit_request(candidate_set_id=grant.candidate_set_id,
                    candidate_id=candidate.poi.poi_id)
                authorized_candidate(state, explicit, self.core.store, self.core.clock())
            try:
                data = selection_input(state, request, grant, "selection_"+uuid4().hex)
            except ValidationError as exc:
                # This is server/request context validation, before any model call.
                raise StateError("INVALID_CHANGE_REQUEST") from exc
            check_deadline()
            attempt = selector.select(data, timeout_seconds=min(selector.timeout_seconds,
                self.deadline_seconds-(self.monotonic()-started)))
            metadata = SelectionMetadata(selection_id=data.selection_id, model=attempt.model,
                candidate_set_id=grant.candidate_set_id,
                selected_candidate_id=attempt.output.candidate_id if attempt.output else None,
                selection_timestamp=self.core.clock(), prompt_schema_version=CANDIDATE_SELECTOR_PROMPT_VERSION,
                selector_policy_version=SELECTOR_POLICY_VERSION, status=attempt.error or "selected",
                candidate_count=len(data.candidates), prompt_characters=attempt.prompt_characters,
                latency_ms=attempt.latency_ms, input_tokens=attempt.input_tokens, output_tokens=attempt.output_tokens,
                rationale=attempt.output.rationale if attempt.output else None)
            check_deadline()
            if attempt.error:
                raise StateError(attempt.error)
            # Reload before grounding: a concurrent writer is never silently rebased.
            current = self.core.loader(request.trip_id)
            authorize_scope(current, request)
            if self.core.store.get(grant.candidate_set_id) != grant:
                raise StateError("LLM_SELECTION_NOT_AUTHORIZED")
            explicit = request.explicit_request(candidate_set_id=grant.candidate_set_id,
                candidate_id=attempt.output.candidate_id)
            authorized_candidate(current, explicit, self.core.store, self.core.clock())
            check_deadline()
            # Copy the service, not mutation logic. The original CAS and all 3B checks remain.
            core = copy.copy(self.core)
            def commit(*args, **kwargs):
                check_deadline()
                return self.core.committer(*args, **kwargs)
            core.committer = commit
            result = core.replan(explicit)
            if result.reason in _AUTH_ERRORS:
                result = result.model_copy(update={"decision": "rejected", "reason": "LLM_SELECTION_NOT_AUTHORIZED"})
            if result.reason.startswith("LLM_SELECTION_"):
                metadata = metadata.model_copy(update={"status": result.reason})
            if result.reason == "LLM_SELECTION_TIMEOUT":
                result = result.model_copy(update={"decision": "unverifiable"})
            return finish(result)
        except (StateError, ValidationError) as exc:
            code = exc.code if isinstance(exc, StateError) else "LLM_SELECTION_INVALID"
            if code in _AUTH_ERRORS:
                code = "LLM_SELECTION_NOT_AUTHORIZED"
            if metadata is not None:
                metadata = metadata.model_copy(update={"status": code})
            decision = "conflict" if code == "VERSION_CONFLICT" else "unverifiable" if code in {
                "LLM_SELECTION_TIMEOUT", "LLM_SELECTION_PROVIDER_ERROR", "REPLAN_UNVERIFIABLE",
                "BASELINE_UNVERIFIABLE"} else "rejected"
            from app.models.replanning import ReplanDecision
            return finish(ReplanDecision(decision=decision, reason=code,
                base_plan_version=request.base_plan_version, scope=scope))
