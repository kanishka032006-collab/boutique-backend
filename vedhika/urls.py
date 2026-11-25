import re

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("store.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif settings.SERVE_MEDIA:
    media_prefix = settings.MEDIA_URL.strip("/")
    if media_prefix:
        urlpatterns += [
            re_path(rf"^{re.escape(media_prefix)}/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
        ]
