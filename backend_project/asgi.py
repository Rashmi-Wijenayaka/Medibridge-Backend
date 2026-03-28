"""
ASGI config for backend_project project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend_project.settings')

# Initialize Django first so app registry is ready before importing websocket routing.
django_asgi_app = get_asgi_application()

import api.routing
from api.ws_auth import TokenAuthMiddleware

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": TokenAuthMiddleware(
        URLRouter(
            api.routing.websocket_urlpatterns
        )
    ),
})