from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView
from mainapp.views.scope_control import DashboardView
from speedpycom.api.health import RootHealthCheckView
from speedpycom.views import default_og_image
from usermodel.views import ProfileEditView
from mainapp.views.profile_images import avatar, team_logo

# Global administration belongs to trusted operators, never tenant roles.
admin.site.has_permission = lambda request: request.user.is_active and request.user.is_superuser
admin.site.site_header = "Scope Control"
urlpatterns = [
    path("", TemplateView.as_view(template_name="mainapp/welcome.html"), name="welcome"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("accounts/profile/", ProfileEditView.as_view(), name="account_profile"),
    path("accounts/profile/avatar/", avatar, name="profile_avatar"),
    path("accounts/profile/avatar/thumbnail/", avatar, {"thumbnail": True}, name="profile_avatar_thumbnail"),
    path("teams/<uuid:team_id>/logo/", team_logo, name="team_logo"),
    path("accounts/", include("allauth.account.urls")),
    path(settings.ADMIN_URL, admin.site.urls),
    path("health/", RootHealthCheckView.as_view(), name="root_health_check"),
    path("og-image.png", default_og_image, name="default-og-image"),
    path("", include("mainapp.urls")),
]
if settings.SPEEDPY_MFA_BACKEND == "allauth_mfa":
    urlpatterns += [path("accounts/2fa/", include("allauth.mfa.urls"))]
