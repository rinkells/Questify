from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        from django.db.models.signals import pre_migrate

        import core.signals  # noqa: F401
        import core.checks  # noqa: F401
        from core.rollback_guards import refuse_unsafe_rollbacks

        # Sent once for every app; with this sender the guards run once.
        pre_migrate.connect(refuse_unsafe_rollbacks, sender=self)
