from django.urls import path

from . import views

urlpatterns = [
    path("staff/home", views.StaffHomeView.as_view()),
    path("staff/classes", views.StaffClassesView.as_view()),
    path("staff/timetable", views.StaffTimetableView.as_view()),
    path("staff/covers/<uuid:cover_id>/note", views.CoverNoteView.as_view()),
    path("staff/me", views.StaffMeView.as_view()),
    path("staff/leave", views.StaffLeaveView.as_view()),
    path("staff/leave/<uuid:leave_id>/cancel", views.StaffLeaveCancelView.as_view()),
    path("staff/documents", views.StaffDocumentsView.as_view()),
    path("staff/students/<uuid:student_id>", views.StaffStudentView.as_view()),
    path("meetings/<uuid:meeting_id>/<str:action>", views.MeetingDecisionView.as_view()),
]
