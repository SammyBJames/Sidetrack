from django.urls import path

from events import views

urlpatterns = [
    path("calendar", views.calendar, name="calendar"),
    path("reload_calendar", views.reload_calendar, name="reload_calendar"),
    path(
        "calendar/event/<int:event_id>.ics",
        views.download_event,
        name="download_event",
    ),
    path(
        "calendar/download/<str:token>",
        views.download_calendar,
        name="download_calendar",
    ),
]
