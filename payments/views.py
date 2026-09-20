"""Views for Paddle payment integration, usage limits, and webhook handling."""

import json
import logging
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from accounts.models import PaymentTransaction, UserSettings, settings_for
from chattydesk.envelope import envelope, error
from payments import paddle_client

logger = logging.getLogger(__name__)
User = get_user_model()


class PaymentConfigView(APIView):
    """GET public Paddle payment configuration (client token, price id, amount)."""

    permission_classes = [AllowAny]

    def get(self, request):
        return envelope(paddle_client.get_paddle_config())


class PaymentStatusView(APIView):
    """GET current caller's subscription plan, request count, and remaining quota."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        st = settings_for(request.user)
        return envelope(
            {
                "is_unlimited": st.is_unlimited,
                "paid_requests_count": st.paid_requests_count,
                "max_free_requests": st.max_free_requests,
                "remaining_paid_requests": st.remaining_paid_requests,
                "plan": "unlimited" if st.is_unlimited else "free",
                "unlocked_at": st.unlocked_at,
                "paddle_transaction_id": st.paddle_transaction_id,
            }
        )


def _find_user_from_payload(data: dict):
    """Extract user instance from Paddle webhook payload custom_data or customer details."""
    custom_data = data.get("custom_data") or {}
    if isinstance(custom_data, str):
        try:
            custom_data = json.loads(custom_data)
        except Exception:
            custom_data = {}

    user_id = custom_data.get("user_id") or custom_data.get("userId")
    if user_id:
        try:
            return User.objects.filter(id=user_id).first()
        except (ValueError, TypeError):
            pass

    username = custom_data.get("username")
    if username:
        user = User.objects.filter(username__iexact=username).first()
        if user:
            return user

    # Check passthrough (Paddle classic / custom passthrough)
    passthrough = data.get("passthrough")
    if passthrough:
        if isinstance(passthrough, str) and passthrough.startswith("{"):
            try:
                pt_json = json.loads(passthrough)
                pt_uid = pt_json.get("user_id") or pt_json.get("userId")
                if pt_uid:
                    user = User.objects.filter(id=pt_uid).first()
                    if user:
                        return user
            except Exception:
                pass
        try:
            user = User.objects.filter(id=int(passthrough)).first()
            if user:
                return user
        except (ValueError, TypeError):
            pass

    # Check customer email
    customer = data.get("customer") or {}
    email = customer.get("email") or data.get("customer_email") or custom_data.get("email")
    if email:
        return User.objects.filter(email__iexact=email).first()

    return None


class PaddleWebhookView(APIView):
    """POST endpoint for Paddle Billing webhooks."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        signature = request.META.get("HTTP_PADDLE_SIGNATURE") or request.headers.get(
            "Paddle-Signature"
        )
        raw_body = request.body

        if not paddle_client.verify_paddle_signature(raw_body, signature):
            logger.warning("Invalid Paddle webhook signature rejected.")
            return error("Invalid signature.", status.HTTP_400_BAD_REQUEST)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return error("Invalid JSON payload.", status.HTTP_400_BAD_REQUEST)

        event_type = payload.get("event_type") or payload.get("alert_name") or ""
        data = payload.get("data") or payload

        # Success event types in Paddle Billing & Classic
        success_events = {
            "transaction.completed",
            "transaction.paid",
            "transaction.billed",
            "subscription.created",
            "subscription.activated",
            "payment_succeeded",
        }

        if event_type and event_type not in success_events:
            # Acknowledge unhandled event types gracefully
            return envelope({"processed": True, "event_type": event_type}, "Event ignored")

        user = _find_user_from_payload(data)
        if not user:
            logger.warning(f"Paddle webhook received but matching user not found for event: {event_type}")
            # Still return 200 so Paddle doesn't retry infinitely
            return envelope({"processed": False, "reason": "User not found"}, "User not found")

        txn_id = data.get("id") or data.get("p_order_id") or f"manual_{timezone.now().timestamp()}"
        customer_id = data.get("customer_id") or (data.get("customer") or {}).get("id", "")

        # Mark user as unlimited
        st = settings_for(user)
        st.is_unlimited = True
        st.paddle_transaction_id = txn_id
        if customer_id:
            st.paddle_customer_id = customer_id
        if not st.unlocked_at:
            st.unlocked_at = timezone.now()
        st.save()

        # Log payment transaction
        totals = (data.get("details") or {}).get("totals") or {}
        total_str = totals.get("total") or data.get("sale_gross") or "0.99"
        currency = (data.get("currency_code") or data.get("p_currency") or "USD").upper()
        try:
            amount = Decimal(str(total_str))
        except Exception:
            amount = Decimal("0.99")

        PaymentTransaction.objects.update_or_create(
            paddle_transaction_id=txn_id,
            defaults={
                "user": user,
                "amount": amount,
                "currency": currency,
                "status": "completed",
                "raw_payload": payload,
            },
        )

        logger.info(f"User {user.username} unlocked unlimited plan via Paddle txn {txn_id}.")
        return envelope({"processed": True, "user_id": user.id, "unlimited": True}, "Payment processed")


class VerifyPaymentView(APIView):
    """POST to verify a completed checkout transaction with Paddle and unlock unlimited access."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        txn_id = (request.data.get("transaction_id") or "").strip()
        if not txn_id:
            return error("Bad Request: 'transaction_id' is required.", status.HTTP_400_BAD_REQUEST)

        # Check Paddle API if key is available
        txn_data = paddle_client.fetch_transaction(txn_id)
        if txn_data:
            txn_status = txn_data.get("status")
            if txn_status not in ("completed", "billed", "paid"):
                return error(
                    f"Transaction is in status '{txn_status}', not completed.",
                    status.HTTP_400_BAD_REQUEST,
                )

        # Unlock user
        st = settings_for(request.user)
        st.is_unlimited = True
        st.paddle_transaction_id = txn_id
        if not st.unlocked_at:
            st.unlocked_at = timezone.now()
        st.save()

        PaymentTransaction.objects.update_or_create(
            paddle_transaction_id=txn_id,
            defaults={
                "user": request.user,
                "amount": Decimal("0.99"),
                "currency": "USD",
                "status": "completed",
                "raw_payload": txn_data or {"transaction_id": txn_id},
            },
        )

        return envelope(
            {
                "is_unlimited": True,
                "paid_requests_count": st.paid_requests_count,
                "max_free_requests": st.max_free_requests,
                "remaining_paid_requests": None,
                "plan": "unlimited",
                "paddle_transaction_id": txn_id,
            },
            "Unlimited plan unlocked",
        )


class TestUnlockView(APIView):
    """Developer helper endpoint to toggle unlimited status in test environments."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        st = settings_for(request.user)
        unlimited = request.data.get("unlimited", True)
        st.is_unlimited = bool(unlimited)
        if st.is_unlimited and not st.unlocked_at:
            st.unlocked_at = timezone.now()
        elif not st.is_unlimited:
            st.unlocked_at = None
        st.save()
        return envelope(
            {
                "is_unlimited": st.is_unlimited,
                "paid_requests_count": st.paid_requests_count,
                "max_free_requests": st.max_free_requests,
                "remaining_paid_requests": st.remaining_paid_requests,
                "plan": "unlimited" if st.is_unlimited else "free",
            },
            "Subscription status updated",
        )
