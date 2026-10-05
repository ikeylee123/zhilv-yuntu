"""Pure Phase 1 validation. No config, storage, network or model imports."""
from __future__ import annotations

from datetime import time
from decimal import Decimal
import re

from app.models.schemas import (
    ConstraintViolation, Itinerary, PlaceReference, PlanningState,
    ScheduledActivity, TripConstraints, TripRequest, ValidationReport,
)


def normalize_name(name: str) -> str:
    # Exact match after case/whitespace normalization; no punctuation removal or fuzzy matching.
    return " ".join(name.split()).casefold()


def parse_time(value: str) -> time:
    if re.fullmatch(r"[0-9]{2}:[0-9]{2}", value) is None:
        raise ValueError("Expected strict HH:MM")
    return time(int(value[:2]), int(value[3:]))


def scheduled_activities(itinerary: Itinerary) -> list[ScheduledActivity]:
    result = []
    for position, day in enumerate(itinerary.days):
        groups = [("attraction", day.spots), ("meal", day.meals),
                  ("hotel", [day.hotel] if day.hotel else []), ("transport", day.transport)]
        for category, items in groups:
            for index, item in enumerate(items):
                result.append(ScheduledActivity(
                    day_index=day.day_index,
                    activity_ref=f"days[{position}].{category}[{index}]",
                    activity_type=category,
                    entity_id=getattr(item, "poi_id", None),
                    name=getattr(item, "name", None) if category != "transport" else
                         f"{item.from_place} -> {item.to_place}",
                    start_time=getattr(item, "start_time", None),
                    end_time=getattr(item, "end_time", None),
                    estimated_cost=Decimal(str(item.estimated_cost)),
                ))
    return result


def constraints_from_request(request: TripRequest) -> TripConstraints:
    supplied = request.constraints
    values = supplied.model_dump(exclude_unset=True) if supplied is not None else {}
    values.setdefault("budget_limit", Decimal(str(request.budget)))
    values.setdefault("soft_preferences", list(request.preferences))
    return TripConstraints.model_validate(values)


def ensure_date_range(request: TripRequest) -> int:
    """Called before either generation path, before any tools or LLM."""
    if request.end_date < request.start_date:
        raise ValueError("end_date must be on or after start_date")
    return (request.end_date - request.start_date).days + 1


def _matches(reference: PlaceReference | str, activity: ScheduledActivity) -> bool:
    if isinstance(reference, PlaceReference):
        if reference.entity_id is not None:
            return reference.entity_id == activity.entity_id
        name = reference.name or ""
    else:
        name = reference
    return bool(normalize_name(name)) and normalize_name(name) == normalize_name(activity.name)


def _identity_uncertain(reference: PlaceReference | str, activity: ScheduledActivity) -> bool:
    if not isinstance(reference, PlaceReference) or reference.entity_id is None or activity.entity_id:
        return False
    # Without a reference name, missing activity IDs cannot be ruled out either.
    name = normalize_name(reference.name or "")
    return not name or not normalize_name(activity.name) or name == normalize_name(activity.name)


def validate_plan(state: PlanningState) -> ValidationReport:
    """Inspect existing estimates and times. Never mutate or repair the plan."""
    plan, constraints, request = state.current_itinerary, state.constraints, state.trip_request
    errors: list[ConstraintViolation] = []
    unknowns: list[ConstraintViolation] = []
    checked = ["date_range", "day_count", "day_date", "day_index", "time_format",
               "time_order", "activity_overlap", "duplicate_attraction"]

    def finding(kind, message, *, severity="error", day=None, activity=None,
                expected=None, actual=None):
        item = ConstraintViolation(type=kind, severity=severity, message=message,
                                   day_index=day, activity_ref=activity,
                                   expected=expected, actual=actual)
        (unknowns if severity == "unknown" else errors).append(item)

    if request is None:
        finding("missing_trip_request", "Original request is unavailable; date constraints cannot be verified.",
                severity="unknown")
    elif request.end_date < request.start_date:
        finding("date_range", "Request end_date precedes start_date.",
                expected=str(request.start_date), actual=str(request.end_date))
    else:
        count = (request.end_date - request.start_date).days + 1
        if len(plan.days) != count:
            finding("day_count", "Itinerary day count differs from the requested date range.",
                    expected=count, actual=len(plan.days))
        indexes = [day.day_index for day in plan.days]
        # Avoid materializing a potentially huge request date range.
        if sorted(indexes) != list(range(1, len(indexes) + 1)):
            finding("day_index", "Day indexes must be unique and consecutive from 1.", actual=indexes)
        for day in plan.days:
            if day.date is None:
                finding("day_date", "Day date is missing.", severity="unknown", day=day.day_index)
            elif not request.start_date <= day.date <= request.end_date:
                finding("day_date", "Day date is outside the requested range.", day=day.day_index,
                        expected=[str(request.start_date), str(request.end_date)], actual=str(day.date))
            elif (day.date - request.start_date).days != day.day_index - 1:
                finding("day_date", "Day date does not agree with its day index.", day=day.day_index,
                        expected=f"start_date + {day.day_index - 1} days", actual=str(day.date))

    activities = scheduled_activities(plan)
    attractions = [a for a in activities if a.activity_type == "attraction"]
    window = {}
    has_window = constraints.daily_start_time is not None or constraints.daily_end_time is not None
    if has_window:
        checked.append("daily_time_window")
        for key in ("daily_start_time", "daily_end_time"):
            value = getattr(constraints, key)
            if value is not None:
                try:
                    window[key] = parse_time(value)
                except ValueError:
                    finding("constraint_time_format", "Daily window requires a valid HH:MM time.",
                            expected="00:00..23:59", actual={key: value})
        if len(window) == 2 and window["daily_start_time"] >= window["daily_end_time"]:
            finding("constraint_time_order", "Daily window start must precede end; overnight windows are not supported.",
                    expected="start < end", actual=[constraints.daily_start_time, constraints.daily_end_time])

    intervals = {}
    for activity in activities:
        times = {}
        for key in ("start_time", "end_time"):
            value = getattr(activity, key)
            if value is not None:
                try:
                    times[key] = parse_time(value)
                except ValueError:
                    finding("time_format", "Activity time is not valid HH:MM.", day=activity.day_index,
                            activity=activity.activity_ref, expected="00:00..23:59", actual={key: value})
        if activity.start_time is None or activity.end_time is None:
            # Attractions are scheduled activities. Other legacy items have no scheduling fields;
            # their missing times become relevant only when a daily window is explicitly requested.
            if activity.activity_type == "attraction" or has_window:
                finding("activity_time_missing", "Activity interval is incomplete; overlap/window cannot be fully checked.",
                        severity="unknown", day=activity.day_index, activity=activity.activity_ref,
                        expected="start_time and end_time", actual=[activity.start_time, activity.end_time])
        if len(times) == 2:
            start, end = times["start_time"], times["end_time"]
            if start >= end:
                finding("time_order", "Activity start must precede end on the same day.",
                        day=activity.day_index, activity=activity.activity_ref,
                        expected="start < end", actual=[activity.start_time, activity.end_time])
            else:
                intervals.setdefault(activity.day_index, []).append((start, end, activity))
        if has_window:
            outside = any(
                ("daily_start_time" in window and value < window["daily_start_time"])
                or ("daily_end_time" in window and value > window["daily_end_time"])
                for value in times.values()
            )
            if outside:
                finding("daily_time_window", "Activity lies outside the requested daily window.",
                        day=activity.day_index, activity=activity.activity_ref,
                        expected=[constraints.daily_start_time, constraints.daily_end_time],
                        actual=[activity.start_time, activity.end_time])

    for day_index, entries in intervals.items():
        entries.sort(key=lambda item: (item[0], item[1], item[2].activity_ref))
        for index, (start, end, activity) in enumerate(entries):
            for next_start, next_end, other in entries[index + 1:]:
                if next_start >= end:
                    break
                finding("activity_overlap", "Two activity intervals overlap (adjacent endpoints are allowed).",
                        day=day_index, activity=other.activity_ref, expected="non-overlapping intervals",
                        actual=[activity.activity_ref, other.activity_ref])

    for index, activity in enumerate(attractions):
        for previous in attractions[:index]:
            if previous.day_index == activity.day_index:
                continue
            same = (previous.entity_id == activity.entity_id
                    if previous.entity_id and activity.entity_id
                    else normalize_name(previous.name) == normalize_name(activity.name))
            if same:
                finding("duplicate_attraction", "Attraction is repeated across days.",
                        day=activity.day_index, activity=activity.activity_ref,
                        expected="unique across days", actual=previous.activity_ref)
                break

    for field, kind in (("must_visit", "must_visit_missing"), ("avoid", "avoided_attraction")):
        references = getattr(constraints, field) or []
        if references:
            checked.append(field)
        for reference in references:
            if isinstance(reference, str) and not normalize_name(reference):
                finding("invalid_place_reference", "Empty place constraints cannot be matched.", actual=field)
                continue
            matches = [a for a in attractions if _matches(reference, a)]
            value = reference.model_dump() if isinstance(reference, PlaceReference) else reference
            uncertain = [a for a in attractions if _identity_uncertain(reference, a)]
            if field == "must_visit" and not matches and not uncertain:
                finding(kind, "Required attraction is absent.", expected=value)
            if uncertain and (field == "avoid" or not matches):
                for activity in uncertain:
                    finding("place_identity_unverifiable", "Missing activity ID prevents verifying a hard place constraint.",
                            severity="unknown", day=activity.day_index, activity=activity.activity_ref,
                            expected={"constraint": field, "place": value},
                            actual={"entity_id": activity.entity_id, "name": activity.name})
            if field == "avoid":
                for match in matches:
                    finding(kind, "Avoided attraction is present.", day=match.day_index,
                            activity=match.activity_ref, expected="absent", actual=value)

    if constraints.budget_limit is not None:
        checked.append("budget_limit")
        # Presence is preserved by the model serializers across API/storage round-trips.
        amounts = []
        for source, field in ((plan, "estimated_budget"), (plan.budget_breakdown, "total")):
            if field in source.model_fields_set:
                amounts.append(Decimal(str(getattr(source, field))))
        if not amounts or not all(amount.is_finite() for amount in amounts):
            finding("budget_unverifiable", "Budget summaries are missing or nonfinite.", severity="unknown")
        else:
            if len(amounts) == 2 and amounts[0] != amounts[1]:
                finding("budget_estimate_mismatch", "Budget totals disagree; the larger estimate is checked.",
                        severity="warning", expected=str(amounts[1]), actual=str(amounts[0]))
            estimate = max(amounts)
            if estimate > constraints.budget_limit:
                finding("budget_exceeded", "Current estimated cost exceeds the limit; this is not real-price validation.",
                        expected=str(constraints.budget_limit), actual=str(estimate))

    required = list(dict.fromkeys(constraints.hard_constraints or []))
    if constraints.transport_preference is not None:
        required.append("transport_preference")
    if constraints.mobility_constraints:
        required.append("mobility_suitability")
    implemented = set(checked)
    for requirement in dict.fromkeys(required):
        if requirement in implemented:
            continue
        checked.append(requirement)
        finding("unverifiable_hard_constraint", "Required constraint lacks supported rules, parameters or external facts.",
                severity="unknown", expected=requirement, actual="not verified in Phase 1")

    return ValidationReport(violations=errors, unknowns=unknowns, checked_constraints=checked)


def attach_validation(itinerary: Itinerary, request: TripRequest | None = None,
                      *, increment_version: bool = False) -> Itinerary:
    """Attach authoritative derived report; keep the original request across edits/saves."""
    original = request if request is not None else itinerary.original_request
    constraints = (constraints_from_request(request) if request is not None else
                   itinerary.constraints if itinerary.constraints is not None else
                   constraints_from_request(original) if original is not None else TripConstraints())
    if request is None and itinerary.constraints is not None and original is not None:
        # Partial/empty overrides inherit the preserved request; explicit null still wins.
        values = constraints_from_request(original).model_dump()
        values.update(itinerary.constraints.model_dump())
        constraints = TripConstraints.model_validate(values)
    version = (itinerary.planning_version or 1) + int(increment_version)
    state = PlanningState(trip_request=original, constraints=constraints,
                          current_itinerary=itinerary, version=version)
    state.validation_report = validate_plan(state)
    itinerary.original_request = original.model_copy(deep=True) if original else None
    itinerary.constraints = constraints.model_copy(deep=True)
    itinerary.validation_report = state.validation_report
    itinerary.planning_version = version
    return itinerary
