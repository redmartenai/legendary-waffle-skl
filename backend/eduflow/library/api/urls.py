from django.urls import path

from . import views

urlpatterns = [
    path("library/settings", views.SettingsView.as_view(), name="library-settings"),
    path("library/books", views.BookList.as_view(), name="book-list"),
    path("library/books/<uuid:pk>", views.BookDetail.as_view(), name="book-detail"),
    path("library/books/<uuid:pk>/copies", views.BookCopiesView.as_view(), name="book-copies"),
    path("library/copies/<uuid:pk>/status", views.CopyStatusView.as_view(), name="copy-status"),
    path("library/loans", views.LoanList.as_view(), name="loan-list"),
    path("library/loans/<uuid:pk>", views.LoanDetail.as_view(), name="loan-detail"),
    path("library/loans/<uuid:pk>/return", views.LoanReturnView.as_view(), name="loan-return"),
    path("library/loans/<uuid:pk>/renew", views.LoanRenewView.as_view(), name="loan-renew"),
    path("library/loans/<uuid:pk>/fine-paid", views.LoanFinePaidView.as_view(), name="loan-fine-paid"),
]
