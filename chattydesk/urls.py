"""
URL configuration for chattydesk project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include

from openrouter_handler.legacy import legacy_urlpatterns

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include([
        path("auth/", include("accounts.urls")),
        path("openrouter/", include("openrouter_handler.urls")),
        # Deprecated: kept so the existing frontend keeps working during the
        # switch to /api/v1/openrouter/. See FRONTEND_HANDOVER.md.
        *legacy_urlpatterns(),
    ])),
]
