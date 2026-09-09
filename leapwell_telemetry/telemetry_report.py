"""Aggregates telemetry rows into the five reported metric groups.

Ported from `leapfrog-ai-service`'s `app/services/telemetry_report.py`
(PR #55, https://github.com/Medtronic-LABS/leapfrog-ai-service/pull/55) --
kept **pure** exactly as the source is: no `frappe` import, no database, no
clock. Takes row-like dicts, returns a dict. This is what makes it testable
without a running site.

This MUST stay semantically identical to the source (and to
`lib/core/telemetry/telemetry_report.dart` in uhis_lf_mobile, which the
source itself is required to match): the same date range asked of any of the
three implementations has to produce the same numbers. The output key names
deliberately match the source/mobile shape for the same reason.

Decisions carried over from the source, each for a reason:

* **median, not mean** for durations -- the client measures wall-clock, so a
  visit left open while the app was backgrounded is an inevitable outlier.
* **rates are ``None``, never 0**, when the denominator is empty. A visit
  where AI Scribe was never started has no capture rate; rendering that as
  ``0%`` would understate the tool. Callers must render ``None`` as a dash.
* **user cohorts are disjoint** -- an SK who used scribe even once is an
  adopter, so the two counts sum to the distinct SKs seen.
* **per-field accuracy is worst-first**, ties broken by volume, so a 1-of-1
  field cannot outrank a 20-of-40 one.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

EVENT_VISIT_COMPLETED = "visit_completed"
EVENT_COUNSELLING_SHARE = "counselling_share"

CHANNEL_SMS = "sms"
CHANNEL_WHATSAPP = "whatsapp"

# Where a share tap happened. Only the counselling surfaces answer "did the SK
# share the counselling message"; the contact surfaces are direct patient
# contact and would inflate that metric if counted together.
SURFACE_COUNSELLING = "counselling"
SURFACE_VISIT_FLOW = "visitFlow"
COUNSELLING_SURFACES = frozenset({SURFACE_COUNSELLING, SURFACE_VISIT_FLOW})

# Caveats travel WITH the numbers. Each one exists because the metric is
# routinely misread without it.
CAVEAT_CORRECTION = (
	"Lower bound: fields the SK never reviewed count as unchanged, so real "
	"error may be higher."
)
CAVEAT_COMPOSE = "Counts compose sheets opened, not messages delivered."
CAVEAT_DURATION = "Wall-clock median; includes time the app spent in the background."
CAVEAT_PROVENANCE = (
	"Prefilled and computed values are neither AI nor SK effort, so they are "
	"counted separately from what the SK manually entered."
)
CAVEAT_DISAGREEMENT = (
	"Corrections plus proposals rejected because the SK had already filled the "
	"field -- a truer disagreement measure than correction rate alone."
)


def _median(values: Sequence[int]) -> Optional[int]:
	"""Median, or None when empty. Even-length averages the two middles."""
	if not values:
		return None
	ordered = sorted(values)
	mid = len(ordered) // 2
	if len(ordered) % 2 == 1:
		return ordered[mid]
	return round((ordered[mid - 1] + ordered[mid]) / 2)


def _pct(numerator: int, denominator: int) -> Optional[float]:
	"""Percentage, or None when there is no denominator (never 0.0)."""
	if denominator == 0:
		return None
	return numerator / denominator * 100


def _strings(raw: Any) -> list[str]:
	return [str(x) for x in raw] if isinstance(raw, list) else []


def build_report(from_dt: Any, to_dt: Any, events: Iterable[Any]) -> dict:
	"""Aggregate [events] into the report payload.

	[events] are row-likes exposing ``event_type``, ``sk_user_id`` and
	``payload`` -- a `Telemetry Event` Document/`_dict`, or a plain dict in
	tests. ``payload`` is expected to already be a parsed dict (Frappe's JSON
	fieldtype returns one; a test can just pass a dict literal).
	"""
	visit_count = scribe_visits = manual_visits = 0
	total_ai_fields = 0
	total_visible = total_rendered = total_library = 0
	ai_filled = ai_corrected = ai_unchanged = 0
	manual_fields = prefilled_fields = derived_fields = ai_overridden = 0
	sms_opened = whatsapp_opened = 0
	contact_sms = contact_whatsapp = 0

	scribe_users: set[str] = set()
	all_users: set[str] = set()
	scribe_durations: list[int] = []
	manual_durations: list[int] = []
	filled_by_field: dict[str, int] = {}
	corrected_by_field: dict[str, int] = {}

	for event in events:
		event_type = _get(event, "event_type")
		payload = _get(event, "payload") or {}

		if event_type == EVENT_VISIT_COMPLETED:
			visit_count += 1
			user = (_get(event, "sk_user_id") or "").strip()
			if user:
				all_users.add(user)

			fields = payload.get("fields") or {}
			denominators = payload.get("denominators") or {}
			filled = _strings(fields.get("aiFilled"))
			corrected = _strings(fields.get("aiCorrected"))
			unchanged = _strings(fields.get("aiAcceptedUnchanged"))
			# Absent on payload v1 rows -- default to empty rather than treat
			# a missing key as a meaningful zero.
			manual = _strings(fields.get("manual"))
			prefilled = _strings(fields.get("prefilled"))
			derived = _strings(fields.get("derived"))
			overridden = _strings(fields.get("aiOverridden"))
			duration = payload.get("durationMs")

			if payload.get("scribeUsed") is True:
				scribe_visits += 1
				if user:
					scribe_users.add(user)
				if isinstance(duration, int):
					scribe_durations.append(duration)
			else:
				manual_visits += 1
				if isinstance(duration, int):
					manual_durations.append(duration)

			total_ai_fields += len(filled)
			ai_filled += len(filled)
			ai_corrected += len(corrected)
			ai_unchanged += len(unchanged)
			manual_fields += len(manual)
			prefilled_fields += len(prefilled)
			derived_fields += len(derived)
			ai_overridden += len(overridden)
			total_visible += int(denominators.get("extractableVisible") or 0)
			total_rendered += int(denominators.get("renderedTotal") or 0)
			total_library += int(denominators.get("libraryTotal") or 0)

			for field_id in filled:
				filled_by_field[field_id] = filled_by_field.get(field_id, 0) + 1
			for field_id in corrected:
				corrected_by_field[field_id] = corrected_by_field.get(field_id, 0) + 1

		elif event_type == EVENT_COUNSELLING_SHARE:
			if payload.get("launched") is not True:
				continue  # a blocked tap never opened a compose sheet
			channel = payload.get("channel")
			# Absent on payloads written before the field existed -- those
			# all came from the counselling screen.
			surface = payload.get("surface") or SURFACE_COUNSELLING
			is_counselling = surface in COUNSELLING_SURFACES
			if channel == CHANNEL_SMS:
				if is_counselling:
					sms_opened += 1
				else:
					contact_sms += 1
			elif channel == CHANNEL_WHATSAPP:
				if is_counselling:
					whatsapp_opened += 1
				else:
					contact_whatsapp += 1
		# Any other event_type is ignored: an older deployment reading rows
		# written by a newer client must not lose the whole range to one
		# unrecognised type.

	per_field = sorted(
		(
			{
				"fieldId": field_id,
				"filled": filled,
				"corrected": corrected_by_field.get(field_id, 0),
				"ratePct": _pct(corrected_by_field.get(field_id, 0), filled),
			}
			for field_id, filled in filled_by_field.items()
		),
		key=lambda f: (-(f["ratePct"] or 0), -f["corrected"]),
	)

	return {
		"from": from_dt,
		"to": to_dt,
		"adoption": {
			"visits": visit_count,
			"scribeVisits": scribe_visits,
			"manualVisits": manual_visits,
			"usersUsingScribe": len(scribe_users),
			"usersManualOnly": len(all_users - scribe_users),
		},
		"timeToComplete": {
			"scribeMedianMs": _median(scribe_durations),
			"manualMedianMs": _median(manual_durations),
			"scribeSamples": len(scribe_durations),
			"manualSamples": len(manual_durations),
			"caveat": CAVEAT_DURATION,
		},
		"fieldsCaptured": {
			"totalAiFields": total_ai_fields,
			"avgPerScribeVisit": (total_ai_fields / scribe_visits if scribe_visits else None),
			"captureRatePct": _pct(total_ai_fields, total_visible),
			"denominators": {
				"extractableVisible": total_visible,
				"rendered": total_rendered,
				"library": total_library,
			},
		},
		"provenance": {
			"manual": manual_fields,
			"prefilled": prefilled_fields,
			"derived": derived_fields,
			"aiOverridden": ai_overridden,
			# Corrections + overridden proposals, over every proposal AI made.
			# Sees the "SK typed first, AI disagreed" case that correction
			# rate structurally cannot.
			"disagreementRatePct": _pct(ai_corrected + ai_overridden, ai_filled + ai_overridden),
			"caveat": CAVEAT_PROVENANCE + " " + CAVEAT_DISAGREEMENT,
		},
		"correction": {
			"aiFilled": ai_filled,
			"corrected": ai_corrected,
			"unchanged": ai_unchanged,
			"ratePct": _pct(ai_corrected, ai_filled),
			"caveat": CAVEAT_CORRECTION,
			"perField": per_field,
		},
		"counselling": {
			"smsComposeOpened": sms_opened,
			"whatsappComposeOpened": whatsapp_opened,
			"contactSmsOpened": contact_sms,
			"contactWhatsappOpened": contact_whatsapp,
			"caveat": CAVEAT_COMPOSE,
		},
	}


def _get(row: Any, key: str) -> Any:
	"""Reads [key] off a dict, a Frappe Document, or any other row-like --
	whichever shape the caller (real query result vs. test fixture) passes."""
	if isinstance(row, dict):
		return row.get(key)
	return getattr(row, key, None)
