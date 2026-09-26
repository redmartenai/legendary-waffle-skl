from django.urls import include, path

from . import views

urlpatterns = [
    path("principal/pulse", views.PulseView.as_view()),
    path("principal/attendance", views.PrincipalAttendanceView.as_view()),
    path("principal/cover", views.CoverView.as_view()),
    path("principal/absentees/message", views.AbsenteeMessageView.as_view()),
    path("principal/sections", views.SectionsView.as_view()),
    path("", include("apps.principal.console.urls")),
]
