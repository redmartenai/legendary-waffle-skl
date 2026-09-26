from django.urls import path

from . import views

urlpatterns = [
    path("platform/overview", views.OverviewView.as_view()),
    path("platform/schools", views.SchoolsView.as_view()),
    path("platform/schools/check-code", views.CheckCodeView.as_view()),
    path("platform/schools/<uuid:pk>", views.SchoolDetailView.as_view()),
    path("platform/schools/<uuid:pk>/people", views.SchoolPeopleView.as_view()),
    path("platform/schools/<uuid:pk>/people/<uuid:user_id>/credentials", views.CredentialsView.as_view()),
    path("auth/invite/<str:token>", views.InviteView.as_view()),
]
