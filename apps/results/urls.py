from django.urls import path

from . import views

urlpatterns = [
    path("students/<uuid:student_id>/results", views.StudentResultsView.as_view()),
]
