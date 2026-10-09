from django.urls import path

from . import views

urlpatterns = [
    # Recipient endpoints first: their literal paths must not be read as an invitation ID.
    path("invitations/preview", views.InvitationPreviewView.as_view(), name="invitation-preview"),
    path(
        "invitations/verification", views.InvitationVerificationView.as_view(), name="invitation-verification"
    ),
    path("invitations/accept", views.InvitationAcceptView.as_view(), name="invitation-accept"),
    path("invitations", views.InvitationList.as_view(), name="invitation-list"),
    path("invitations/<uuid:pk>", views.InvitationDetail.as_view(), name="invitation-detail"),
    path("invitations/<uuid:pk>/resend", views.InvitationResendView.as_view(), name="invitation-resend"),
    path("invitations/<uuid:pk>/revoke", views.InvitationRevokeView.as_view(), name="invitation-revoke"),
]
