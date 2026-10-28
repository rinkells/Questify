import pkgutil

from django.apps import apps
from django.core.checks import Tags, Warning, register
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder


@register(Tags.database)
def check_achievement_configs(app_configs, **kwargs):
    """Report achievements that can never be unlocked.

    The admin validates condition configs, but rows written another way
    (shell, scripts, fixtures, data saved before validation was stricter)
    are not, and the evaluators then quietly treat them as never met. The
    container start runs ``check --database default``, so this shows up in
    the startup log. Configs are judged as the evaluators read them, not by
    the admin's stricter validation, so only rows that really never unlock
    are reported.
    """
    from .conditions import EVALUATOR_REGISTRY, condition_config_problem
    from .models import Achievement

    if (
        app_configs is not None
        and apps.get_app_config('achievements') not in app_configs
    ):
        return []
    warnings = []
    for alias in kwargs.get('databases') or ():
        if _migrations_pending(connections[alias]):
            # migrate runs database checks before migrating, when the table
            # may be missing or hold rows a pending data migration (0003)
            # is about to fix.
            continue
        for achievement in Achievement.objects.using(alias).order_by('id'):
            if achievement.condition_type not in EVALUATOR_REGISTRY:
                hint = (
                    f'Unknown condition type "{achievement.condition_type}". '
                    'Choose a supported condition_type in the admin.'
                )
            else:
                problem = condition_config_problem(
                    achievement.condition_type,
                    achievement.condition_config,
                )
                if problem is None:
                    continue
                hint = f'{problem} Fix condition_config in the admin.'
            warnings.append(Warning(
                f'Achievement {achievement.pk} ("{achievement.name}") can '
                'never be unlocked.',
                hint=hint,
                obj=achievement,
                id='achievements.W001',
            ))
    return warnings


def _migrations_pending(connection):
    """Whether any achievements migration on disk is not yet applied.

    Compares file names with the migration records instead of building a
    MigrationExecutor, which loads every app's migration graph on each check.
    """
    from . import migrations

    on_disk = {
        name
        for _, name, is_package in pkgutil.iter_modules(migrations.__path__)
        # The names Django's MigrationLoader skips.
        if not is_package and name[0] not in '_~'
    }
    applied = {
        name
        for app_label, name in MigrationRecorder(connection).applied_migrations()
        if app_label == 'achievements'
    }
    return bool(on_disk - applied)
