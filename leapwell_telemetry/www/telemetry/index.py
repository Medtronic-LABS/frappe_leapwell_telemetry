"""Gate for the /telemetry dashboard shell.

Mirrors leapfrog-ai-service's `_require_dashboard_enabled` (PR #55): the page
is safe to serve without auth because it contains no numbers -- its own JS
calls `get_report` with an X-Auth-Token + tenantId the viewer supplies once.
This gate exists only so a deployment that hasn't opted into the internal
tooling doesn't advertise it at all.
"""

import frappe


def get_context(context):
	settings = frappe.get_single("Leapwell Telemetry Settings")
	if not settings.enable_dashboard:
		raise frappe.PageDoesNotExistError
	context.no_cache = 1
