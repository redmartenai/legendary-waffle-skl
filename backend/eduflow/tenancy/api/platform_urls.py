from django.urls import path

from . import platform

urlpatterns = [
    path("platform/schools", platform.PlatformSchoolListView.as_view(), name="platform-school-list"),
    path(
        "platform/schools/<uuid:school_id>",
        platform.PlatformSchoolDetailView.as_view(),
        name="platform-school-detail",
    ),
    path(
        "platform/users/<uuid:user_id>",
        platform.PlatformUserDetailView.as_view(),
        name="platform-user-detail",
    ),
]
