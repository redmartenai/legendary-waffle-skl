from django.urls import path

from . import views

urlpatterns = [
    path("chat/contacts", views.ChatContactsView.as_view()),
    path("chat/conversations", views.ConversationListView.as_view()),
    path("chat/conversations/<uuid:conversation_id>/messages", views.MessageListView.as_view()),
    path("chat/conversations/<uuid:conversation_id>/read", views.ConversationReadView.as_view()),
]
