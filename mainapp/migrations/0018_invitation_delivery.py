from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("mainapp", "0017_scope_control_data_lifecycle")]
    operations = [
        migrations.AddField(
            model_name="teaminvitation",
            name="delivery_status",
            field=models.CharField(
                max_length=16,
                default="unknown",
                choices=[
                    ("pending", "Ожидает постановки в очередь"),
                    ("queued", "Ожидает обработки"),
                    ("sending", "Отправка начата; результат пока неизвестен"),
                    ("sent", "Почтовая обработка завершена"),
                    ("failed", "Отправка не подтверждена"),
                    ("unknown", "Результат прежней отправки неизвестен"),
                ],
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="teaminvitation",
            name="delivery_updated_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="teaminvitation",
            name="delivery_attempt",
            field=models.UUIDField(null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="teaminvitation",
            name="delivery_status",
            field=models.CharField(
                max_length=16,
                default="pending",
                choices=[
                    ("pending", "Ожидает постановки в очередь"),
                    ("queued", "Ожидает обработки"),
                    ("sending", "Отправка начата; результат пока неизвестен"),
                    ("sent", "Почтовая обработка завершена"),
                    ("failed", "Отправка не подтверждена"),
                    ("unknown", "Результат прежней отправки неизвестен"),
                ],
            ),
        ),
    ]
