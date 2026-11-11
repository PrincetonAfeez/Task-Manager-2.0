from django.urls import path

from . import views, views_status

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
    path("version", views.version, name="version"),
    path("healthz/worker", views.worker_healthz, name="worker_healthz"),
    path("healthz/scheduler", views.scheduler_healthz, name="scheduler_healthz"),
    path("healthz/reaper", views.reaper_healthz, name="reaper_healthz"),
    path("status", views_status.status_page, name="status_page"),
    path("status/", views_status.status_page),
]
