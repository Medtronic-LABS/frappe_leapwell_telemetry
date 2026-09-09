"""Add compound indexes on Telemetry Event -- matches the (tenant_id,
occurred_at) and (tenant_id, event_type) indexes in the source
telemetry_events table (leapfrog-ai-service PR #55). Frappe doctype JSON has
no first-class way to declare a multi-column index, so this is the correct
seam for it."""

import frappe


def execute():
	frappe.db.add_index("Telemetry Event", ["tenant_id", "occurred_at"])
	frappe.db.add_index("Telemetry Event", ["tenant_id", "event_type"])
