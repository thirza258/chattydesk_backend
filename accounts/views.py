"""Username/password accounts issuing JWT pairs.

Endpoints (all under /api/v1/auth/):

    POST register/  username, password[, email]  -> access + refresh + user
    POST login/     username, password           -> access + refresh + user
    POST refresh/   refresh                      -> access
    GET  me/        Authorization: Bearer <access> -> user

`access` is short-lived and goes on every API call; `refresh` buys a new one
without asking for the password again. Signing out is the client dropping both
tokens — there is no server-side token store to keep in sync.
"""

from django.contrib.auth import authenticate
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import settings_for
from accounts.serializers import (
    RegisterSerializer,
    SettingsSerializer,
    serialize_settings,
    serialize_user,
)
from chattydesk.envelope import envelope, error, first_error
from openrouter_handler import client


class AuthThrottle(AnonRateThrottle):
    """Rate for the unauthenticated endpoints — see DEFAULT_THROTTLE_RATES."""

    scope = "auth"


class PublicAPIView(APIView):
    """Base for the endpoints a signed-out visitor has to be able to reach.

    `authentication_classes` is emptied deliberately. With the project-wide
    JWTAuthentication in place, a *stale* bearer token would otherwise be
    rejected with 401 before the view ran — which is precisely the moment
    someone needs to log in or refresh.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]


def _token_pair(user):
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": serialize_user(user),
    }


class Register(PublicAPIView):
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if not serializer.is_valid():
            return error(
                f"Bad Request: {first_error(serializer.errors)}",
                status.HTTP_400_BAD_REQUEST,
            )

        user = serializer.save()
        return envelope(
            _token_pair(user), "Account created", status.HTTP_201_CREATED
        )


class Login(PublicAPIView):
    def post(self, request):
        username = str(request.data.get("username") or "").strip()
        password = str(request.data.get("password") or "")

        if not username or not password:
            return error(
                "Bad Request: 'username' and 'password' are required.",
                status.HTTP_400_BAD_REQUEST,
            )

        # authenticate() also refuses inactive accounts.
        user = authenticate(request, username=username, password=password)
        if user is None:
            # One message for an unknown username and a wrong password alike:
            # telling them apart hands an attacker a list of valid usernames.
            return error(
                "Invalid username or password.", status.HTTP_401_UNAUTHORIZED
            )

        return envelope(_token_pair(user), "Signed in")


class Refresh(PublicAPIView):
    def post(self, request):
        token = request.data.get("refresh")
        if not token:
            return error(
                "Bad Request: 'refresh' field is required.",
                status.HTTP_400_BAD_REQUEST,
            )

        try:
            refresh = RefreshToken(token)
        except TokenError:
            return error(
                "Your session has expired. Sign in again.",
                status.HTTP_401_UNAUTHORIZED,
            )

        return envelope({"access": str(refresh.access_token)}, "Token refreshed")


class Me(APIView):
    """Who the bearer token belongs to.

    The frontend calls this on start-up to decide whether a stored token is
    still worth trusting. Authentication and IsAuthenticated come from the
    project-wide REST_FRAMEWORK defaults.
    """

    def get(self, request):
        return envelope(serialize_user(request.user))


def _key_check(info):
    """OpenRouter's own view of a key — trimmed to what the settings page shows."""
    return {
        "label": info.get("label") or "",
        "usage": info.get("usage"),
        "limit": info.get("limit"),
        "limit_remaining": info.get("limit_remaining"),
        "is_free_tier": info.get("is_free_tier"),
    }


class Settings(APIView):
    """The caller's preferences, including a personal OpenRouter key.

        GET   -> {has_own_key, key_hint, prefer_own_key, updated_at}
        PATCH -> any of {openrouter_key, prefer_own_key}

    Send `openrouter_key: ""` to remove a saved key. A new key is checked
    against OpenRouter before it is stored, so a typo is caught here rather than
    in the middle of a conversation.
    """

    def get(self, request):
        return envelope(serialize_settings(settings_for(request.user)))

    def patch(self, request):
        row = settings_for(request.user)
        serializer = SettingsSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return error(
                f"Bad Request: {first_error(serializer.errors)}",
                status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        checked = None

        if "openrouter_key" in data:
            raw_key = data["openrouter_key"] or ""
            if raw_key:
                try:
                    checked = client.key_info(raw_key)
                except client.OpenRouterError as exc:
                    if exc.status_code in (401, 403):
                        return error(
                            "OpenRouter rejected that key. Check it at openrouter.ai/keys.",
                            status.HTTP_400_BAD_REQUEST,
                        )
                    # OpenRouter being unreachable is no reason to refuse a key
                    # that is probably fine; it just goes unverified.
                    checked = None
            row.set_openrouter_key(raw_key)

            # Removing the key leaves "always use mine" pointing at nothing, so
            # it comes off too — unless this same request says otherwise, which
            # the check below then refuses.
            if not raw_key and "prefer_own_key" not in data:
                row.prefer_own_key = False

        if "prefer_own_key" in data:
            row.prefer_own_key = data["prefer_own_key"]

        if row.prefer_own_key and not row.has_own_key:
            # Nothing has been saved yet, so this leaves the row as it was.
            return error(
                "Bad Request: add your OpenRouter key before choosing to use it first.",
                status.HTTP_400_BAD_REQUEST,
            )

        row.save()

        payload = serialize_settings(row)
        if checked is not None:
            payload["key_check"] = _key_check(checked)
        return envelope(payload, "Settings saved")

    # PUT is accepted as an alias so a client that does not send PATCH still works.
    put = patch
