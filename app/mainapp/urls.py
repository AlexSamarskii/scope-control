from django.conf import settings
from django.urls import path
from mainapp import views
from mainapp.views import team_members
from mainapp.views.scope_control import (ProjectListView, ProjectCreateView, ProjectDetailView, ProjectRenameView, ScopeConfirmView)

from mainapp.views.workspace import ProjectArchiveView, DemoProjectView

urlpatterns = []
if getattr(settings, "SPEEDPY_MFA_BACKEND", "django_otp") == "django_otp":
    urlpatterns += [
        # OTP Management URLs
        path('accounts/otp/settings/', views.OTPSettingsView.as_view(), name='account_otp_settings'),
        path('accounts/otp/setup/', views.OTPSetupView.as_view(), name='account_otp_setup'),
        path('accounts/otp/verify-setup/', views.OTPVerifySetupView.as_view(), name='account_otp_verify_setup'),
        path('accounts/otp/backup-codes/', views.OTPBackupCodesView.as_view(), name='account_otp_backup_codes'),
        path('accounts/otp/disable/', views.OTPDisableView.as_view(), name='account_otp_disable'),
        path('accounts/otp/regenerate-backup-codes/', views.OTPRegenerateBackupCodesView.as_view(), name='account_otp_regenerate_backup_codes'),

        # OTP Login URL
        path('accounts/login/otp/', views.OTPLoginView.as_view(), name='account_login_otp'),
    ]

if getattr(settings, "SPEEDPY_TEAMS_ENABLED", True):
    urlpatterns += [
        path('teams/create/', views.TeamCreateView.as_view(), name='team_create'),
        path('teams/<uuid:team_id>/dashboard/', ProjectListView.as_view(), name='team_dashboard'),
        path('teams/<uuid:team_id>/settings/', views.TeamSettingsView.as_view(), name='team_settings'),
        path('teams/<uuid:team_id>/delete/', views.TeamDeleteView.as_view(), name='team_delete'),
        path('teams/<uuid:team_id>/delete/cancel/', views.TeamDeleteCancelView.as_view(), name='team_delete_cancel'),

        # Team member management
        path('teams/<uuid:team_id>/members/', team_members.TeamMembersListView.as_view(), name='team_members'),
        path('teams/<uuid:team_id>/members/invite/', team_members.InviteMemberView.as_view(), name='invite_member'),
        path('teams/<uuid:team_id>/members/<uuid:membership_id>/update-role/', team_members.UpdateMemberRoleView.as_view(), name='update_member_role'),
        path('teams/<uuid:team_id>/members/<uuid:membership_id>/remove/', team_members.RemoveMemberView.as_view(), name='remove_member'),

        path('teams/<uuid:team_id>/invitations/<uuid:invitation_id>/resend/', team_members.ResendInvitationView.as_view(), name='resend_invitation'),

        # Public invitation URLs (with token)
        path('teams/invitations/<str:token>/accept/', team_members.AcceptInvitationView.as_view(), name='accept_invitation'),
        path('teams/invitations/<str:token>/decline/', team_members.DeclineInvitationView.as_view(), name='decline_invitation'),
        path('teams/<uuid:team_id>/invitations/<uuid:invitation_id>/revoke/', team_members.RevokeInvitationView.as_view(), name='revoke_invitation'),

    ]
    urlpatterns += [
        path("teams/<uuid:team_id>/projects/", ProjectListView.as_view(), name="project_list"),
        path("teams/<uuid:team_id>/projects/demo/", DemoProjectView.as_view(), name="demo_project"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/archive/", ProjectArchiveView.as_view(), name="project_archive"),
        path("teams/<uuid:team_id>/projects/new/", ProjectCreateView.as_view(), name="project_create"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/", ProjectDetailView.as_view(), name="project_detail"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/edit/", ProjectRenameView.as_view(), name="project_rename"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/confirm/", ScopeConfirmView.as_view(), name="scope_confirm"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/versions/<uuid:version_id>/", ProjectDetailView.as_view(), name="scope_version"),
    ]

if getattr(settings, "SPEEDPY_TEAMS_ENABLED", True):
    from mainapp.views.analysis import (
        RequestCreateView,
        RequestDetailView,
        RequestAnalyzeView,
    )

    urlpatterns += [
        path(
            "teams/<uuid:team_id>/projects/<uuid:pk>/requests/new/",
            RequestCreateView.as_view(),
            name="request_create",
        ),
        path(
            "teams/<uuid:team_id>/projects/<uuid:pk>/requests/<uuid:request_id>/",
            RequestDetailView.as_view(),
            name="request_detail",
        ),
        path(
            "teams/<uuid:team_id>/projects/<uuid:pk>/requests/<uuid:request_id>/analyze/",
            RequestAnalyzeView.as_view(),
            name="request_analyze",
        ),
    ]

if getattr(settings, "SPEEDPY_TEAMS_ENABLED", True):
    from mainapp.views.change_control import (
        ChangeDecisionView, ChangeDetailView, ChangeReviseView, ChangeEventView,
        ChangeApplyView, ChangeDraftView, ChangeExportView,
    )
    urlpatterns += [
        path("teams/<uuid:team_id>/projects/<uuid:pk>/requests/<uuid:request_id>/decision/new/", ChangeDecisionView.as_view(), name="change_create"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/changes/<uuid:card_id>/", ChangeDetailView.as_view(), name="change_detail"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/changes/<uuid:card_id>/revise/", ChangeReviseView.as_view(), name="change_revise"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/changes/<uuid:card_id>/events/", ChangeEventView.as_view(), name="change_event"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/changes/<uuid:card_id>/apply/", ChangeApplyView.as_view(), name="change_apply"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/changes/<uuid:card_id>/revisions/<uuid:revision_id>/draft.txt", ChangeDraftView.as_view(), name="change_draft"),
        path("teams/<uuid:team_id>/projects/<uuid:pk>/changes.csv", ChangeExportView.as_view(), name="change_export"),
    ]

if getattr(settings, "SPEEDPY_TEAMS_ENABLED", True):
    from mainapp.views.data_lifecycle import ProjectDeleteView, ProjectDeletionStatusView
    urlpatterns += [
        path("teams/<uuid:team_id>/projects/<uuid:pk>/delete/",ProjectDeleteView.as_view(),name="project_delete"),
        path("teams/<uuid:team_id>/project-deletions/<uuid:receipt_id>/",ProjectDeletionStatusView.as_view(),name="project_deletion_status"),
    ]
