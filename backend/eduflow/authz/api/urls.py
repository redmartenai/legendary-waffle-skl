from django.urls import path

from . import views

urlpatterns = [
    path("permissions", views.PermissionCatalogView.as_view(), name="permission-list"),
    path("me/permissions", views.MyPermissionsView.as_view(), name="my-permissions"),
    path("roles", views.RoleListView.as_view(), name="role-list"),
    path("roles/<uuid:role_id>", views.RoleDetailView.as_view(), name="role-detail"),
]
