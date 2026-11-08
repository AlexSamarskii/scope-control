"""Analysis tasks carry identifiers only; no document/model result in Redis."""

import structlog
from uuid import UUID
from celery import shared_task
from mainapp.services.analysis import execute_run, reconcile_analysis


@shared_task(name="scope_analysis.run", acks_late=True, ignore_result=True)
def run_scope_analysis(run_id):
    try:
        key = UUID(str(run_id))
    except (ValueError, TypeError, AttributeError):
        return None
    try:
        execute_run(key)
    except Exception:
        # Do not let Celery format a database/adapter exception containing data.
        structlog.get_logger(__name__).warning(
            "scope_analysis_task_failed", run_id=str(key)
        )
    return None


@shared_task(name="scope_analysis.reconcile", ignore_result=True)
def reconcile_scope_analysis():
    try:
        reconcile_analysis()
    except Exception:
        structlog.get_logger(__name__).warning("scope_analysis_reconcile_failed")
    return None
