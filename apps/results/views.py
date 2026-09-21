from rest_framework.response import Response

from apps.academics.access import student_for_request
from apps.core.api import SchoolAPIView

from .services import student_results


class StudentResultsView(SchoolAPIView):
    def get(self, request, student_id):
        return Response(student_results(student_for_request(request, student_id)))
