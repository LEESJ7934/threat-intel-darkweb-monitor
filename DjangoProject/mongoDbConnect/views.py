import logging

from django.shortcuts import render
from django.views.decorators.http import require_safe

from . import dashboard

LOGGER = logging.getLogger(__name__)


@require_safe
def latest_data_table(request):
    filters, errors = dashboard.parse_filters(request.GET)
    context = {**dashboard.paginate([], filters), **dashboard.summary_data([]),
               "filters": filters, "filter_errors": errors, "risk_options": dashboard.RISK_LEVELS,
               "source_options": [], "database_error": None, "candidate_truncated": False,
               "scan_limit": dashboard.MAX_SCAN}
    if not errors:
        try:
            with dashboard.mongo_database() as database:
                context.update(dashboard.fetch_dashboard(database, filters, secrets=dashboard.configured_secrets()))
        except dashboard.DashboardUnavailable:
            context["database_error"] = dashboard.DATABASE_ERROR
            LOGGER.warning("Dashboard data unavailable")
    return render(request, "mongoDbConnect/table.html", context)


@require_safe
def event_detail(request, document_id):
    context = {"event": None, "history": [], "alerts": [], "database_error": None, "not_found": False}
    status = 200
    try:
        dashboard.decode_id(document_id)  # Reject invalid IDs before opening MongoDB.
        with dashboard.mongo_database() as database:
            context.update(dashboard.fetch_detail(database, document_id, secrets=dashboard.configured_secrets()))
    except dashboard.EventNotFound:
        context["not_found"] = True
        status = 404
    except dashboard.DashboardUnavailable:
        context["database_error"] = dashboard.DATABASE_ERROR
        LOGGER.warning("Event detail data unavailable")
    return render(request, "mongoDbConnect/detail.html", context, status=status)
