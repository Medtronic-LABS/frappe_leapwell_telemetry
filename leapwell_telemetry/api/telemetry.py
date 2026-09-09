"""
AI Scribe / counselling telemetry: batch ingest, aggregated report, and a
settings-gated destructive delete.

  leapwell_telemetry.api.telemetry.ingest_events    SK app batch-uploads telemetry
  leapwell_telemetry.api.telemetry.get_report        aggregated report for a date range
  leapwell_telemetry.api.telemetry.delete_events     dashboard-gated, wipes a tenant's telemetry

Ported from leapfrog-ai-service's app/api/telemetry.py (PR #55,
https://github.com/Medtronic-LABS/leapfrog-ai-service/pull/55) -- same three
operations and the same idempotent-ingest / tenant-scoping design, adapted to
this platform's whitelist(remote_auth=True) convention (POST-only,
X-Auth-Token + tenantId headers) instead of FastAPI's UserContext dependency.
No Provider/identity resolution step is needed here (unlike
shukhee_integration.api.consultation) -- telemetry is scoped by tenant only;
sk_user_id is a client-supplied grouping key, never an identity claim.
"""

from datetime import datetime, time, timedelta, timezone

import frappe
from frappe import _
from frappe.utils import cint, get_datetime

from leapwell_telemetry import telemetry_report
from uhis_next_core.auth.decorators import current_remote_tenant_id, whitelist

_MAX_EVENTS_PER_BATCH = 500


def _resolve_env(payload=None):
	"""Same dual-mode parsing as shukhee_integration.api.consultation --
	accepts either a legacy `payload` string or the raw JSON body merged into
	form_dict."""
	if payload:
		return frappe.parse_json(payload)
	return frappe.local.form_dict


def _tenant_id():
	tenant_id = current_remote_tenant_id()
	if not tenant_id:
		frappe.throw(_("A tenantId header is required."), frappe.ValidationError)
	return tenant_id


def _require_dashboard_enabled():
	if not frappe.get_single("Leapwell Telemetry Settings").enable_dashboard:
		frappe.throw(_("This feature is not enabled."), frappe.ValidationError)


@whitelist(methods=["POST"], remote_auth=True)
def ingest_events(payload=None):
	"""Stores events, skipping any client_uuid already held.

	`tenant_id` is taken from the authenticated caller's tenantId header --
	any tenant field in the payload is ignored, so a client cannot attribute
	its events to someone else's tenant.

	`accepted_ids` returns every id now stored, duplicates included. That
	matters: a client whose previous response was lost re-sends the same
	batch, and if the reply only listed newly-inserted ids it would never
	learn those rows are safe and would resend them forever.
	"""
	env = _resolve_env(payload)
	events = env.get("events") or []
	if not isinstance(events, list):
		frappe.throw(_("events must be a list."), frappe.ValidationError)
	if len(events) > _MAX_EVENTS_PER_BATCH:
		frappe.throw(
			_("A batch may contain at most {0} events.").format(_MAX_EVENTS_PER_BATCH),
			frappe.ValidationError,
		)

	for event in events:
		if not event.get("id") or not event.get("eventType") or not event.get("occurredAt"):
			frappe.throw(_("Each event requires id, eventType, and occurredAt."), frappe.ValidationError)

	tenant_id = _tenant_id()
	ids = [event["id"] for event in events]
	# One query to find already-stored ids, rather than one exists() check per
	# event -- the common case (a retried batch) is mostly duplicates.
	existing_ids = (
		set(frappe.get_all("Telemetry Event", filters={"client_uuid": ["in", ids]}, pluck="client_uuid"))
		if ids
		else set()
	)

	inserted = 0
	for event in events:
		client_uuid = event["id"]
		if client_uuid in existing_ids:
			continue
		frappe.get_doc(
			{
				"doctype": "Telemetry Event",
				"client_uuid": client_uuid,
				"tenant_id": tenant_id,
				"event_type": event["eventType"],
				"occurred_at": get_datetime(event["occurredAt"]),
				"visit_uuid": event.get("visitUuid"),
				"sk_user_id": event.get("skUserId"),
				"app_version": event.get("appVersion") or "",
				"app_build": cint(event.get("appBuild") or 0),
				"payload_version": cint(event.get("payloadVersion") or 1),
				"payload": event.get("payload") or {},
			}
		).insert(ignore_permissions=True)
		inserted += 1
	frappe.db.commit()

	return {
		"received": len(events),
		"inserted": inserted,
		"duplicates": len(events) - inserted,
		"accepted_ids": ids,
	}


def _day_bounds(from_date, to_date, tz_offset_minutes):
	"""Widens two calendar dates to cover whole days in the viewer's timezone.

	Both ends inclusive: `to_date` runs to 23:59:59.999999, so "1 Sep to 7
	Sep" includes everything that happened on the 7th rather than cutting it
	off at midnight.

	`tz_offset_minutes` exists so the dashboard/mobile and this report agree:
	the mobile client buckets by the device's *local* calendar date, so
	bucketing this report in UTC would disagree with it by the offset."""
	tz = timezone(timedelta(minutes=tz_offset_minutes))
	start = datetime.combine(from_date.date(), time.min, tzinfo=tz)
	end = datetime.combine(to_date.date(), time.max, tzinfo=tz)
	return start, end


@whitelist(methods=["POST"], remote_auth=True)
def get_report(payload=None):
	"""Aggregated telemetry report for a date range, scoped to the caller's
	tenant only. `from`/`to` are dates (YYYY-MM-DD); `tz_offset_minutes`
	widens them to whole days in the caller's timezone (0 = UTC)."""
	env = _resolve_env(payload)
	from_date = env.get("from")
	to_date = env.get("to")
	tz_offset_minutes = cint(env.get("tz_offset_minutes") or 0)
	if not from_date or not to_date:
		frappe.throw(_("'from' and 'to' dates are required."), frappe.ValidationError)

	from_dt = get_datetime(from_date)
	to_dt = get_datetime(to_date)
	if to_dt < from_dt:
		frappe.throw(_("'to' precedes 'from'."), frappe.ValidationError)

	start, end = _day_bounds(from_dt, to_dt, tz_offset_minutes)
	tenant_id = _tenant_id()
	events = frappe.get_all(
		"Telemetry Event",
		filters={"tenant_id": tenant_id, "occurred_at": ["between", [start, end]]},
		fields=["event_type", "sk_user_id", "payload"],
	)
	return telemetry_report.build_report(from_date, to_date, events)


@whitelist(methods=["POST"], remote_auth=True)
def delete_events():
	"""Clears the calling tenant's telemetry so a ground-truth run can be
	compared one visit against one visit.

	Two gates, both deliberate: authentication (so the tenant comes from the
	session, never the caller) and the dashboard feature flag (so a
	deployment that hasn't opted into the internal tooling has no delete
	route at all). Destructive and unrecoverable -- there is no undo."""
	_require_dashboard_enabled()
	tenant_id = _tenant_id()
	names = frappe.get_all("Telemetry Event", filters={"tenant_id": tenant_id}, pluck="name")
	for name in names:
		frappe.delete_doc("Telemetry Event", name, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {"deleted": len(names)}
