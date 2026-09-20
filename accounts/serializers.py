"""Signup validation and the one user shape the API hands back.

There is no custom user model: accounts are `django.contrib.auth.User` rows, so
`createsuperuser` and the admin keep working unchanged.
"""

from django.contrib.auth import get_user_model, password_validation
from rest_framework import serializers

from accounts.models import settings_for

User = get_user_model()


def serialize_user(user):
    st = settings_for(user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "date_joined": user.date_joined,
        "is_unlimited": st.is_unlimited if st else False,
        "paid_requests_count": st.paid_requests_count if st else 0,
        "max_free_requests": st.max_free_requests if st else 50,
        "remaining_paid_requests": st.remaining_paid_requests if st else 50,
    }


def serialize_settings(row):
    """The caller's settings.

    The OpenRouter key is write-only: what comes back is whether one is saved
    and its last four characters, never the key.
    """
    if not row:
        return {}
    return {
        "has_own_key": row.has_own_key,
        "key_hint": row.openrouter_key_hint,
        "prefer_own_key": row.prefer_own_key,
        "is_unlimited": row.is_unlimited,
        "paid_requests_count": row.paid_requests_count,
        "max_free_requests": row.max_free_requests,
        "remaining_paid_requests": row.remaining_paid_requests,
        "unlocked_at": row.unlocked_at,
        "paddle_transaction_id": row.paddle_transaction_id,
        "updated_at": row.updated_at,
    }


class SettingsSerializer(serializers.Serializer):
    """A settings patch: whatever is sent changes, the rest is left alone."""

    openrouter_key = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=200
    )
    prefer_own_key = serializers.BooleanField(required=False)

    def validate_openrouter_key(self, value):
        key = (value or "").strip()
        if not key:
            return ""  # An empty string is how the client clears the key.
        # Catch the obvious paste mistake — an OpenAI key, half a key — before
        # spending a round trip on OpenRouter to be told the same thing.
        if not key.startswith("sk-or-"):
            raise serializers.ValidationError(
                "An OpenRouter key starts with 'sk-or-'. Create one at openrouter.ai/keys."
            )
        return key


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ("username", "email", "password")
        extra_kwargs = {
            "email": {"required": False, "allow_blank": True},
        }

    def validate_username(self, value):
        username = value.strip()
        # The model's unique constraint is case-sensitive, so "Ada" and "ada"
        # would be two accounts that look like one at sign-in time.
        if User.objects.filter(username__iexact=username).exists():
            raise serializers.ValidationError("That username is already taken.")
        return username

    def validate_password(self, value):
        # Runs AUTH_PASSWORD_VALIDATORS from settings — length, common
        # passwords, all-numeric — and reports their messages verbatim.
        password_validation.validate_password(value)
        return value

    def create(self, validated_data):
        # create_user hashes the password; User.objects.create would store the
        # plaintext and let it pass every login.
        return User.objects.create_user(
            username=validated_data["username"],
            email=validated_data.get("email", ""),
            password=validated_data["password"],
        )
