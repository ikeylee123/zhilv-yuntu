"""Bounded selection contracts; no plan mutation authority."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, AwareDatetime, model_validator
from app.models.replanning import PlanChangeRequest, ReplanDecision, ClockTime

Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
CandidateID = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r".*\S.*")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SelectionCandidate(StrictModel):
    candidate_id: CandidateID
    name: Text
    category: Literal["spot"]
    short_description: Text | None = None
    address_or_area: Text | None = None
    provider_type: Text | None = None


class TargetContext(StrictModel):
    name: Text | None = None
    day_theme: Text | None = None
    authorized_start_time: ClockTime | None = None
    authorized_end_time: ClockTime | None = None

    @model_validator(mode="after")
    def authorized_slot(self):
        start, end = self.authorized_start_time, self.authorized_end_time
        if (start is None) != (end is None):
            raise ValueError("Authorized time context must be paired")
        if start is not None and start >= end:
            raise ValueError("Authorized time context must increase within the same day")
        return self


class HardConstraintSummary(StrictModel):
    relevant_must_visit: tuple[Text, ...] = Field(default=(), max_length=20)
    avoid_already_filtered: Literal[True] = True
    authorized_category: Literal["spot"] = "spot"
    budget_validation: Literal["server_only"] = "server_only"


class CandidateSelectionInput(StrictModel):
    selection_id: CandidateID
    operation: Literal["replace", "add"]
    target_context: TargetContext
    hard_constraint_summary: HardConstraintSummary
    soft_preferences: tuple[Text, ...] = Field(default=(), max_length=20)
    candidate_set_id: CandidateID
    candidates: tuple[SelectionCandidate, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({c.candidate_id for c in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate candidate IDs")
        if self.operation == "add" and self.target_context.authorized_start_time is None:
            raise ValueError("Add requires an authorized time slot")
        return self


class CandidateSelectionOutput(StrictModel):
    candidate_id: CandidateID
    rationale: Annotated[str, StringConstraints(strict=True, max_length=1000)] | None = None


class SelectionMetadata(StrictModel):
    mode: Literal["llm"] = "llm"
    selection_id: str
    model: str
    candidate_set_id: str
    selected_candidate_id: str | None = None
    selection_timestamp: AwareDatetime
    prompt_schema_version: str
    selector_policy_version: str
    status: str
    candidate_count: int
    prompt_characters: int = 0
    latency_ms: float = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    rationale: str | None = None


class SelectionDecision(ReplanDecision):
    selection_metadata: SelectionMetadata | None = None


class ReplanSelectionRequest(PlanChangeRequest):
    selection_mode: Literal["explicit", "llm"] = "explicit"
    preference: Literal["attraction", "museum", "park"] = "attraction"

    @model_validator(mode="after")
    def operation_fields(self):
        raw = self.model_dump(exclude={"selection_mode", "preference"})
        if self.selection_mode == "llm":
            if self.candidate_id is not None or self.candidate_set_id is not None:
                raise ValueError("LLM mode prepares its own server candidate set")
            if self.change_type in ("replace", "add"):
                raw.update(candidate_id="pending", candidate_set_id="pending")
        elif self.preference != "attraction":
            raise ValueError("Search preference applies only to LLM mode")
        PlanChangeRequest.model_validate(raw)
        return self

    def explicit_request(self, **selection):
        raw = self.model_dump(exclude={"selection_mode", "preference"})
        return PlanChangeRequest.model_validate(dict(raw, **selection))
