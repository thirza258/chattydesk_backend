from django.apps import AppConfig


class BenchmarkingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "benchmarking"
    verbose_name = "AI Comparison & Benchmarking"

    def ready(self):
        # Seed built-in datasets if needed when the app is ready
        pass
