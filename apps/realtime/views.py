from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from . import client


class RealtimeConnectionView(APIView):
    """Connection details for the realtime server, or ``enabled: false`` to poll instead."""

    def get(self, request):
        if not client.enabled():
            return Response({"enabled": False, "poll_interval_seconds": 5})
        return Response(
            {
                "enabled": True,
                "url": settings.CENTRIFUGO["WS_URL"],
                "token": client.connection_token(request.user),
                "expires_in": 3600,
                "personal_channel": client.personal_channel(request.user.id),
                "poll_interval_seconds": 15,
            }
        )
