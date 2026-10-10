from django.urls import path

from . import views

urlpatterns = [
    path("lms/lessons", views.LessonList.as_view(), name="lesson-list"),
    path("lms/lessons/<uuid:pk>", views.LessonDetail.as_view(), name="lesson-detail"),
    path("lms/lessons/<uuid:pk>/file", views.LessonFileView.as_view(), name="lesson-file"),
    path("lms/lessons/<uuid:pk>/progress", views.LessonProgressView.as_view(), name="lesson-progress"),
    path("lms/quizzes", views.QuizList.as_view(), name="quiz-list"),
    path("lms/quizzes/generate", views.QuizGenerateView.as_view(), name="quiz-generate"),
    path("lms/quizzes/<uuid:pk>", views.QuizDetail.as_view(), name="quiz-detail"),
    path("lms/quizzes/<uuid:pk>/attempts", views.QuizAttemptsView.as_view(), name="quiz-attempts"),
    path("lms/paths", views.PathList.as_view(), name="learning-path-list"),
    path("lms/paths/<uuid:pk>", views.PathDetail.as_view(), name="learning-path-detail"),
    path("lms/paths/<uuid:pk>/progress", views.PathProgressView.as_view(), name="learning-path-progress"),
    path("lms/live-classes", views.LiveList.as_view(), name="live-class-list"),
    path("lms/live-classes/<uuid:pk>", views.LiveDetail.as_view(), name="live-class-detail"),
    path("lms/live-classes/<uuid:pk>/join", views.LiveJoinView.as_view(), name="live-class-join"),
    path("lms/analytics", views.AnalyticsView.as_view(), name="lms-analytics"),
]
