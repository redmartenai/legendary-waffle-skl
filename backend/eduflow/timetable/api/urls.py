from django.urls import path

from . import views

urlpatterns = [
    path("timetables", views.TimetableList.as_view(), name="timetable-list"),
    path("timetables/<uuid:pk>", views.TimetableDetail.as_view(), name="timetable-detail"),
    path("timetables/<uuid:pk>/publish", views.TimetablePublishView.as_view(), name="timetable-publish"),
    path("timetables/<uuid:pk>/archive", views.TimetableArchiveView.as_view(), name="timetable-archive"),
    path("timetables/<uuid:pk>/copy", views.TimetableCopyView.as_view(), name="timetable-copy"),
    path("timetable-periods", views.PeriodList.as_view(), name="timetable-period-list"),
    path("timetable-periods/<uuid:pk>", views.PeriodDetail.as_view(), name="timetable-period-detail"),
    path("timetable-slots", views.SlotList.as_view(), name="timetable-slot-list"),
    path("timetable-slots/<uuid:pk>", views.SlotDetail.as_view(), name="timetable-slot-detail"),
    path("lessons", views.LessonList.as_view(), name="lesson-list"),
    path("lessons/<uuid:pk>", views.LessonDetail.as_view(), name="lesson-detail"),
    path("schedule/me", views.MyScheduleView.as_view(), name="schedule-me"),
    path("staff/<uuid:pk>/schedule", views.StaffScheduleView.as_view(), name="staff-schedule"),
    path("students/<uuid:pk>/schedule", views.StudentScheduleView.as_view(), name="student-schedule"),
    path("sections/<uuid:pk>/schedule", views.SectionScheduleView.as_view(), name="section-schedule"),
]
