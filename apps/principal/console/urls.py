from django.urls import path

from . import (
    academics,
    admissions,
    approvals,
    attendance,
    common,
    communication,
    dashboard,
    documents,
    exams,
    fees,
    reports,
    settings,
    staff,
    students,
    timetable,
    transport,
)

urlpatterns = [
    path("console/context", common.ContextView.as_view()),
    path("console/search", common.SearchView.as_view()),
]
for page in (dashboard, students, admissions, staff, academics, timetable, exams, attendance, fees, transport, approvals, communication, documents, reports, settings):
    urlpatterns += page.urlpatterns
