"""
Unit tests for leapwell_telemetry.telemetry_report.build_report.

A representative subset ported from leapfrog-ai-service's
tests/test_telemetry_report.py (PR #55) -- that file itself is a port of
uhis_lf_mobile's telemetry_report_test.dart. All three must agree, because
reconciling the same date range across implementations is the strongest
check that ingest is faithful. Pure logic, no database -- runs as plain
unittest, same as uhis_next_core/tests/test_remote_auth.py's non-DB cases.
"""

import unittest

from leapwell_telemetry.telemetry_report import (
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
	EVENT_COUNSELLING_SHARE,
	EVENT_VISIT_COMPLETED,
	build_report,
)

FROM = "2026-09-01"
TO = "2026-09-07"


class _Row:
	"""Stands in for a Telemetry Event row — same duck type build_report consumes."""

	def __init__(self, event_type, payload, sk_user_id=None):
		self.event_type = event_type
		self.payload = payload
		self.sk_user_id = sk_user_id


def _visit(
	*,
	scribe_used,
	sk_user_id="sk-1",
	duration_ms=None,
	corrected=(),
	unchanged=(),
	manual=(),
	visible=0,
	rendered=0,
	library=0,
):
	payload = {
		"scribeUsed": scribe_used,
		"fields": {
			"aiFilled": [*corrected, *unchanged],
			"aiCorrected": list(corrected),
			"aiAcceptedUnchanged": list(unchanged),
			"manual": list(manual),
		},
		"denominators": {
			"libraryTotal": library,
			"renderedTotal": rendered,
			"extractableVisible": visible,
		},
	}
	if duration_ms is not None:
		payload["durationMs"] = duration_ms
	return _Row(EVENT_VISIT_COMPLETED, payload, sk_user_id)


def _share(channel, launched=True, surface="counselling"):
	return _Row(
		EVENT_COUNSELLING_SHARE,
		{"channel": channel, "surface": surface, "launched": launched},
	)


def _build(events):
	return build_report(FROM, TO, events)


class TestAdoption(unittest.TestCase):
	def test_splits_visits_and_counts_distinct_users_per_cohort(self):
		report = _build(
			[
				_visit(scribe_used=True, sk_user_id="sk-a"),
				_visit(scribe_used=True, sk_user_id="sk-a"),
				_visit(scribe_used=False, sk_user_id="sk-b"),
				_visit(scribe_used=False, sk_user_id="sk-c"),
			]
		)["adoption"]

		self.assertEqual(report["visits"], 4)
		self.assertEqual(report["scribeVisits"], 2)
		self.assertEqual(report["manualVisits"], 2)
		self.assertEqual(report["usersUsingScribe"], 1)
		self.assertEqual(report["usersManualOnly"], 2)

	def test_one_scribe_visit_makes_an_sk_an_adopter_not_manual_only(self):
		# The cohorts must stay disjoint or the totals double-count.
		report = _build(
			[
				_visit(scribe_used=True, sk_user_id="sk-a"),
				_visit(scribe_used=False, sk_user_id="sk-a"),
			]
		)["adoption"]

		self.assertEqual(report["usersUsingScribe"], 1)
		self.assertEqual(report["usersManualOnly"], 0)


class TestDuration(unittest.TestCase):
	def test_medians_per_cohort_with_sample_counts(self):
		report = _build(
			[
				_visit(scribe_used=True, duration_ms=100),
				_visit(scribe_used=True, duration_ms=300),
				_visit(scribe_used=True, duration_ms=200),
				_visit(scribe_used=False, duration_ms=600),
				_visit(scribe_used=False, duration_ms=400),
			]
		)["timeToComplete"]

		self.assertEqual(report["scribeMedianMs"], 200)
		self.assertEqual(report["scribeSamples"], 3)
		self.assertEqual(report["manualMedianMs"], 500)  # even count -> mean of middles
		self.assertEqual(report["manualSamples"], 2)

	def test_median_resists_a_backgrounded_app_outlier(self):
		report = _build(
			[
				_visit(scribe_used=True, duration_ms=100),
				_visit(scribe_used=True, duration_ms=120),
				_visit(scribe_used=True, duration_ms=40_000_000),
			]
		)["timeToComplete"]

		self.assertEqual(report["scribeMedianMs"], 120)

	def test_null_median_when_no_visit_carried_a_duration(self):
		report = _build([_visit(scribe_used=True)])["timeToComplete"]
		self.assertIsNone(report["scribeMedianMs"])
		self.assertEqual(report["scribeSamples"], 0)


class TestFieldsCaptured(unittest.TestCase):
	def test_totals_average_and_capture_rate(self):
		report = _build(
			[
				_visit(scribe_used=True, unchanged=["systolic", "diastolic"], visible=10, rendered=24, library=33),
				_visit(scribe_used=True, unchanged=["weight"], corrected=["hemoglobin"], visible=10, rendered=24, library=33),
			]
		)["fieldsCaptured"]

		self.assertEqual(report["totalAiFields"], 4)
		self.assertEqual(report["avgPerScribeVisit"], 2.0)
		self.assertEqual(report["captureRatePct"], 20.0)
		self.assertEqual(
			report["denominators"], {"extractableVisible": 20, "rendered": 48, "library": 66}
		)

	def test_capture_rate_is_none_not_zero_with_no_visible_fields(self):
		report = _build([_visit(scribe_used=False, manual=["a"])])["fieldsCaptured"]
		self.assertIsNone(report["captureRatePct"])
		self.assertIsNone(report["avgPerScribeVisit"])


class TestCorrection(unittest.TestCase):
	def test_correction_rate_is_corrected_over_filled(self):
		report = _build(
			[_visit(scribe_used=True, corrected=["weight"], unchanged=["systolic", "diastolic", "pulse"])]
		)["correction"]

		self.assertEqual(report["aiFilled"], 4)
		self.assertEqual(report["corrected"], 1)
		self.assertEqual(report["unchanged"], 3)
		self.assertEqual(report["ratePct"], 25.0)

	def test_correction_rate_is_none_when_ai_filled_nothing(self):
		report = _build([_visit(scribe_used=False, manual=["a"])])["correction"]
		self.assertIsNone(report["ratePct"])

	def test_per_field_accuracy_ranks_worst_first(self):
		report = _build(
			[
				_visit(scribe_used=True, corrected=["hemoglobin"], unchanged=["systolic"]),
				_visit(scribe_used=True, corrected=["hemoglobin"], unchanged=["systolic"]),
				_visit(scribe_used=True, unchanged=["systolic", "weight"]),
			]
		)["correction"]

		worst = report["perField"][0]
		self.assertEqual(worst["fieldId"], "hemoglobin")
		self.assertEqual(worst["filled"], 2)
		self.assertEqual(worst["corrected"], 2)
		self.assertEqual(worst["ratePct"], 100.0)

	def test_a_high_volume_field_outranks_a_one_of_one_at_the_same_rate(self):
		events = [_visit(scribe_used=True, corrected=["bulk"]) for _ in range(10)]
		events.append(_visit(scribe_used=True, corrected=["oneoff"]))

		report = _build(events)["correction"]

		self.assertEqual(report["perField"][0]["fieldId"], "bulk")

	def test_the_correction_caveat_travels_with_the_number(self):
		report = _build([_visit(scribe_used=True, corrected=["weight"])])["correction"]
		self.assertIn("lower bound", report["caveat"].lower())


class TestCounselling(unittest.TestCase):
	def test_counts_sms_and_whatsapp_separately(self):
		report = _build([_share(CHANNEL_SMS), _share(CHANNEL_SMS), _share(CHANNEL_WHATSAPP)])["counselling"]
		self.assertEqual(report["smsComposeOpened"], 2)
		self.assertEqual(report["whatsappComposeOpened"], 1)

	def test_a_tap_that_never_opened_a_compose_sheet_is_not_counted(self):
		report = _build([_share(CHANNEL_SMS, launched=False)])["counselling"]
		self.assertEqual(report["smsComposeOpened"], 0)

	def test_share_events_do_not_inflate_the_visit_count(self):
		report = _build([_visit(scribe_used=True), _share(CHANNEL_SMS)])
		self.assertEqual(report["adoption"]["visits"], 1)

	def test_compose_caveat_never_says_sent(self):
		report = _build([_share(CHANNEL_SMS)])["counselling"]
		self.assertIn("not messages delivered", report["caveat"])
		self.assertNotIn("sent", report["caveat"].lower())


class TestEmptyRangeAndForwardCompat(unittest.TestCase):
	def test_empty_range_yields_zeros_and_none_rates(self):
		report = _build([])

		self.assertEqual(report["adoption"]["visits"], 0)
		self.assertIsNone(report["fieldsCaptured"]["captureRatePct"])
		self.assertIsNone(report["correction"]["ratePct"])
		self.assertEqual(report["correction"]["perField"], [])
		self.assertEqual(report["counselling"]["smsComposeOpened"], 0)

	def test_an_unknown_event_type_is_ignored_rather_than_raising(self):
		# An older deployment reading rows written by a newer client must not
		# lose the whole range to one unrecognised type.
		unknown = _Row("something_future", {"whatever": True})
		report = _build([_visit(scribe_used=True), unknown])
		self.assertEqual(report["adoption"]["visits"], 1)

	def test_a_row_with_a_missing_payload_does_not_raise(self):
		report = _build([_Row(EVENT_VISIT_COMPLETED, None)])
		self.assertEqual(report["adoption"]["visits"], 1)
		self.assertEqual(report["adoption"]["manualVisits"], 1)

	def test_report_shape_is_pinned(self):
		"""The exact key set the dashboard relies on -- a missing group would
		blank a card. Pinned literally, same rationale as the source test."""
		report = _build([])
		self.assertEqual(
			set(report),
			{"from", "to", "adoption", "timeToComplete", "fieldsCaptured", "provenance", "correction", "counselling"},
		)
		self.assertEqual(
			set(report["counselling"]),
			{"smsComposeOpened", "whatsappComposeOpened", "contactSmsOpened", "contactWhatsappOpened", "caveat"},
		)


if __name__ == "__main__":
	unittest.main()
