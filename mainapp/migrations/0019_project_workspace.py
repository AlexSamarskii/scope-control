from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("mainapp", "0018_invitation_delivery")]
    operations = [
        migrations.AddField(
            model_name="project",
            name="archived_at",
            field=models.DateTimeField(null=True, blank=True, editable=False),
        ),
        migrations.AddField(
            model_name="project",
            name="is_demo",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddConstraint(
            model_name="project",
            constraint=models.UniqueConstraint(
                fields=("team",),
                condition=models.Q(is_demo=True, deletion_requested_at__isnull=True),
                name="one_live_demo_per_team",
            ),
        ),
    ]
