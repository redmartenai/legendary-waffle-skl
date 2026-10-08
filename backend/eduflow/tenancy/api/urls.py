from django.urls import path

from . import views

urlpatterns = [
    path("schools/lookup", views.SchoolLookupView.as_view(), name="school-lookup"),
    path("school", views.CurrentSchoolView.as_view(), name="school-current"),
    path("memberships", views.MembershipListView.as_view(), name="membership-list"),
    path("memberships/<uuid:membership_id>", views.MembershipDetailView.as_view(), name="membership-detail"),
    path(
        "memberships/<uuid:membership_id>/roles",
        views.MembershipRoleListView.as_view(),
        name="membership-roles",
    ),
    path(
        "memberships/<uuid:membership_id>/roles/<uuid:role_id>",
        views.MembershipRoleDetailView.as_view(),
        name="membership-role-detail",
    ),
]
