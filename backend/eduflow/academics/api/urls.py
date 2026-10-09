from django.urls import path

from . import views

# Grades are the school's academic levels; the client's "/classes/{id}" already means a section (ADR-007).
urlpatterns = [
    path("campuses", views.CampusList.as_view(), name="campus-list"),
    path("campuses/<uuid:pk>", views.CampusDetail.as_view(), name="campus-detail"),
    path("academic-years", views.AcademicYearList.as_view(), name="academic-year-list"),
    path("academic-years/<uuid:pk>", views.AcademicYearDetail.as_view(), name="academic-year-detail"),
    path("departments", views.DepartmentList.as_view(), name="department-list"),
    path("departments/<uuid:pk>", views.DepartmentDetail.as_view(), name="department-detail"),
    path("grades", views.GradeList.as_view(), name="grade-list"),
    path("grades/<uuid:pk>", views.GradeDetail.as_view(), name="grade-detail"),
    path("sections", views.SectionList.as_view(), name="section-list"),
    path("sections/<uuid:pk>", views.SectionDetail.as_view(), name="section-detail"),
    path("subjects", views.SubjectList.as_view(), name="subject-list"),
    path("subjects/<uuid:pk>", views.SubjectDetail.as_view(), name="subject-detail"),
]
