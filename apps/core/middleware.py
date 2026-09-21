from .tenant import unscoped


class AdminUnscopedMiddleware:
    """Django admin is an internal platform tool, so it runs across all schools.

    Access is limited to users with ``is_staff`` (EduFlow employees), never school users.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/admin/"):
            with unscoped():
                return self.get_response(request)
        return self.get_response(request)
