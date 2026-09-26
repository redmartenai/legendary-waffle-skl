from datetime import timedelta

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
from .services import authenticate_password, request_otp, verify_otp


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
        "email": user.email or None,
        "language": user.language,
        "preferences": preferences_of(user),
        "platform": user.is_staff,
        "must_change_password": user.must_change_password,
    }


# Defaults for notification channels and attendance alerts; stored values override them.
DEFAULT_PREFERENCES = {
    "channels": {"push": True, "sms": True, "whatsapp": False, "email": True},
    "alerts": {"not_in_by": True, "late_arrival": True},
}


def preferences_of(user) -> dict:
    stored = user.preferences or {}
    return {key: {**defaults, **(stored.get(key) or {})} for key, defaults in DEFAULT_PREFERENCES.items()}


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
    # Optional: without it the code signs the person in to every school they belong to.
    school_code = serializers.CharField(max_length=16, required=False, allow_blank=True)
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
        school = None
        school_code = (data.validated_data.get("school_code") or "").strip().upper()
        if school_code:
            school = School.objects.filter(code=school_code, is_active=True).first()
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
        return Response(session_payload(user))


# Without "keep me signed in", a web session ends after a working day.
SHORT_REFRESH = timedelta(hours=12)


def session_payload(user, remember: bool = True) -> dict:
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    refresh = RefreshToken.for_user(user)
    if not remember:
        refresh.set_exp(lifetime=SHORT_REFRESH)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": user_payload(user),
        "memberships": memberships_payload(user),
    }


class PasswordLoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False)
    school_code = serializers.CharField(max_length=16, required=False, allow_blank=True)
    remember = serializers.BooleanField(required=False, default=False)


class PasswordLoginView(APIView):
    """Email-or-phone + password sign-in, used by the Principal web app."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_login"

    def post(self, request):
        data = PasswordLoginSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        school = None
        code = (data.validated_data.get("school_code") or "").strip().upper()
        if code:
            school = School.objects.filter(code=code, is_active=True).first()
            if school is None:
                return Response(
                    {"error": {"code": "not_found", "message": "We couldn't find that school."}},
                    status=status.HTTP_404_NOT_FOUND,
                )
        user = authenticate_password(data.validated_data["identifier"], data.validated_data["password"], school)
        return Response(session_payload(user, remember=data.validated_data["remember"]))


class PasswordChangeSerializer(serializers.Serializer):
    current = serializers.CharField(max_length=128, trim_whitespace=False)
    new = serializers.CharField(min_length=10, max_length=128, trim_whitespace=False)


class PasswordChangeView(APIView):
    """Replace a password: required after signing in with a temporary one."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_login"

    def post(self, request):
        data = PasswordChangeSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(data.validated_data["current"]):
            raise serializers.ValidationError({"current": "That isn't your current password."})
        new = data.validated_data["new"]
        if new == data.validated_data["current"]:
            raise serializers.ValidationError({"new": "Pick a password different from the temporary one."})
        from django.contrib.auth.password_validation import validate_password

        try:
            validate_password(new, user)
        except Exception as exc:  # django.core.exceptions.ValidationError
            raise serializers.ValidationError({"new": list(getattr(exc, "messages", [str(exc)]))}) from None
        user.set_password(new)
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        return Response({"user": user_payload(user)})


class MeView(APIView):
    """The signed-in person and every school/role they can switch between."""

    def get(self, request):
        return Response({"user": user_payload(request.user), "memberships": memberships_payload(request.user)})

    def patch(self, request):
        prefs = request.data.get("preferences")
        if prefs is not None:
            if not isinstance(prefs, dict):
                raise serializers.ValidationError({"preferences": "Send an object."})
            merged = preferences_of(request.user)
            for key, values in prefs.items():
                if key not in DEFAULT_PREFERENCES or not isinstance(values, dict):
                    raise serializers.ValidationError({"preferences": f"Unknown setting group: {key}."})
                for name, value in values.items():
                    if name not in DEFAULT_PREFERENCES[key] or not isinstance(value, bool):
                        raise serializers.ValidationError({"preferences": f"Unknown or invalid setting: {key}.{name}."})
                    merged[key][name] = value
            request.user.preferences = merged
            request.user.save(update_fields=["preferences"])
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
