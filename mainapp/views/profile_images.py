"""Only own avatar / current company logo, never an arbitrary media path."""

from io import BytesIO
from PIL import Image, UnidentifiedImageError
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from mainapp.services.scope_control import membership_for

MAX_IMAGE_BYTES = 5 * 1024 * 1024
FALLBACK = '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><rect width="96" height="96" rx="48" fill="#5145e5"/><circle cx="48" cy="35" r="16" fill="white"/><path d="M20 80a28 28 0 0 1 56 0" fill="white"/></svg>'


def image_response(field):
    try:
        if not field:
            raise ValueError
        with field.storage.open(field.name, "rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError
        with Image.open(BytesIO(data)) as img:
            fmt = img.format
            img.verify()
        content_type = {
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "WEBP": "image/webp",
            "GIF": "image/gif",
        }[fmt]
        response = HttpResponse(data, content_type=content_type)
    except (
        OSError,
        ValueError,
        KeyError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ):
        response = HttpResponse(FALLBACK, content_type="image/svg+xml")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_safe
@never_cache
def avatar(request, thumbnail=False):
    return image_response(
        request.user.profile_picture_thumbnail
        if thumbnail
        else request.user.profile_picture
    )


@login_required
@require_safe
@never_cache
def team_logo(request, team_id):
    membership = membership_for(request.user, team_id)
    return image_response(membership.team.logo)
