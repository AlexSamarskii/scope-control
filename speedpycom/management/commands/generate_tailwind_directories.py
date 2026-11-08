# django management command
from django.conf import settings
from django.core.management.base import BaseCommand
from django.apps import apps
import os
import json


class Command(BaseCommand):
    help = 'Generate tailwind directories'

    def handle(self, *args, **kwargs):
        installed_apps = apps.get_app_configs()
        template_directories = [f'{app.path}/templates/**/*.' + '{html,js}' for app in installed_apps if
                                os.path.exists(f'{app.path}/templates')]
        static_directories = [f'{app.path}/static/**/*.js' for app in installed_apps if
                              os.path.exists(f'{app.path}/static')]
        all_directories = template_directories + static_directories
        with open(os.path.join(settings.BASE_DIR, 'tailwind_directories.json'), 'w') as f:
            json.dump(all_directories, f)
        self.stdout.write(self.style.SUCCESS('Successfully generated tailwind directories'))
