"""Lightweight health endpoints for Kubernetes probes.

- /healthz/  -> liveness. Cheap, never touches the DB, so a transient database
                outage does not cause Kubernetes to kill every backend pod.
- /readyz/   -> readiness. Verifies the DB is reachable; a failing pod is pulled
                out of the Service endpoints until the DB recovers.
"""

from django.db import connection
from django.http import JsonResponse


def healthz(request):
    return JsonResponse({"status": "ok"})


def readyz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - report any DB failure as not-ready
        return JsonResponse({"status": "unavailable", "detail": str(exc)}, status=503)
    return JsonResponse({"status": "ready"})
