from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.tenant import unscoped
from apps.tenancy.models import School
from apps.tenancy.views import school_public

from .models import Membership, PushDevice
from .services import request_otp, verify_otp


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    return forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")


def user_payload(user) -> dict:
    return {
        "id": str(user.id),
        "full_name": user.full_name,
        "first_name": user.first_name,
        "initials": user.initials,
        "phone": user.phone,
        "language": user.language,
    }


def memberships_payload(user) -> list[dict]:
    with unscoped():
        memberships = (
            Membership.all_objects.select_related("school")
            .filter(user=user, is_active=True, school__is_active=True)
            .order_by("school__name", "role")
        )
        grouped: dict = {}
        for membership in memberships:
            entry = grouped.setdefault(
                membership.school_id, {"school": school_public(membership.school), "roles": []}
            )
            entry["roles"].append(
                {
                    "role": membership.role,
                    "title": membership.title or membership.get_role_display(),
                    "department": membership.department or None,
                }
            )
    return list(grouped.values())


class OtpRequestSerializer(serializers.Serializer):
    school_code = serializers.CharField(max_length=16)
    phone = serializers.CharField(max_length=20)


class OtpVerifySerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    code = serializers.CharField(max_length=8)


class OtpRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_request"

    def post(self, request):
        data = OtpRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        school = School.objects.filter(
            code=data.validated_data["school_code"].strip().upper(), is_active=True
        ).first()
        if school is None:
            return Response(
                {"error": {"code": "not_found", "message": "We couldn't find that school."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        challenge, code = request_otp(school, data.validated_data["phone"], _client_ip(request))
        body = {
            "challenge_id": str(challenge.id),
            "expires_in": settings.EDUFLOW["OTP_TTL_SECONDS"],
            "resend_in": 30,
        }
        if code and settings.EDUFLOW["OTP_DEV_ECHO"]:
            body["dev_code"] = code
        return Response(body, status=status.HTTP_201_CREATED)


class OtpVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_verify"

    def post(self, request):
        data = OtpVerifySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        user = verify_otp(data.validated_data["challenge_id"], data.validated_data["code"])
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": user_payload(user),
                "memberships": memberships_payload(user),
            }
        )


class MeView(APIView):
    """The signed-in person and every school/role they can switch between."""

    def get(self, request):
        return Response({"user": user_payload(request.user), "memberships": memberships_payload(request.user)})

    def patch(self, request):
        language = request.data.get("language")
        if language:
            if language not in {"en", "hi", "te"}:
                return Response(
                    {"error": {"code": "invalid", "message": "Unsupported language."}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            request.user.language = language
            request.user.save(update_fields=["language"])
        return Response({"user": user_payload(request.user)})


class PushDeviceSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=255)
    platform = serializers.ChoiceField(choices=PushDevice.Platform.choices)
    app_variant = serializers.CharField(max_length=20, required=False, default="main")


class PushDeviceView(APIView):
    def post(self, request):
        data = PushDeviceSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        # A shared family phone moves the token to whoever signed in last.
        PushDevice.objects.update_or_create(
            token=data.validated_data["token"],
            defaults={
                "user": request.user,
                "platform": data.validated_data["platform"],
                "app_variant": data.validated_data["app_variant"],
                "is_active": True,
                "last_seen_at": timezone.now(),
            },
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def delete(self, request):
        token = request.data.get("token") or request.query_params.get("token")
        if token:
            PushDevice.objects.filter(user=request.user, token=token).update(is_active=False)
        return Response(status=status.HTTP_204_NO_CONTENT)
