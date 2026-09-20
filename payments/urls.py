from django.urls import path
from payments import views

urlpatterns = [
    path("config/", views.PaymentConfigView.as_view(), name="payment-config"),
    path("status/", views.PaymentStatusView.as_view(), name="payment-status"),
    path("webhook/", views.PaddleWebhookView.as_view(), name="paddle-webhook"),
    path("verify/", views.VerifyPaymentView.as_view(), name="payment-verify"),
    path("test-unlock/", views.TestUnlockView.as_view(), name="payment-test-unlock"),
]
