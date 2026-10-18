"""Background jobs without Celery: the schedule, the job runner and the job registry.

Celery (app.workers) and the sync daemon (app.services.sync.daemon) run the same jobs
through these modules; nothing here imports Celery or kombu, so the API and the daemon
can use them on an install without the worker extra.
"""
