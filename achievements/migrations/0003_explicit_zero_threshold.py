from django.db import migrations


def add_explicit_zero_threshold(apps, schema_editor):
    """Write out the old default for achievements saved without a threshold.

    A missing threshold used to mean 0. It now makes the achievement
    unattainable and the admin requires one, so without this, rows that
    simply relied on that default would stop being earned after the deploy,
    while users who already earned them keep them.

    Only configs with no keys besides the ones the condition reads are
    filled in. Anything else, such as {'treshold': 3} or {'count': 5} on a
    condition that reads "threshold", is a misconfigured real threshold:
    writing 0 next to it would grant the achievement to everyone and hide the
    mistake from the admin. Those stay unattainable and are reported by the
    achievements.W001 check until someone fixes them.
    """
    Achievement = apps.get_model('achievements', 'Achievement')
    for achievement in Achievement.objects.iterator():
        config = achievement.condition_config
        if not isinstance(config, dict):
            continue
        # Mirrors validate_condition_config at the time of this migration.
        keys = ('threshold', 'count') if achievement.condition_type in (
            'quest_count',
            'quest_count_by_category',
        ) else ('threshold',)
        if any(key in config for key in keys):
            continue
        other_keys = {'category'} if (
            achievement.condition_type == 'quest_count_by_category'
        ) else set()
        if not set(config) <= other_keys:
            continue
        achievement.condition_config = {**config, 'threshold': 0}
        achievement.save(update_fields=('condition_config',))


class Migration(migrations.Migration):

    dependencies = [
        ('achievements', '0002_alter_achievement_condition_type'),
    ]

    operations = [
        migrations.RunPython(
            add_explicit_zero_threshold,
            migrations.RunPython.noop,
        ),
    ]
