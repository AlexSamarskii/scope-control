from django import forms
from django.core.files.uploadedfile import UploadedFile


def validate_display_image(value):
    if not isinstance(value, UploadedFile):
        return value
    if value.size > 5 * 1024 * 1024:
        raise forms.ValidationError("Изображение должно быть не больше 5 МБ.")
    # ImageField already verified the format. Bound decoded processing as well.
    image = getattr(value, "image", None)
    if image is None or image.width * image.height > 20_000_000:
        raise forms.ValidationError(
            "Изображение должно содержать не больше 20 миллионов пикселей."
        )
    return value
