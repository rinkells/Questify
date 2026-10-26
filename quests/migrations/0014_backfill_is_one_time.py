from django.db import migrations
from django.db.models import Min


def flag_one_time_completions(apps, schema_editor):
    """Mark pre-existing completed rows of one-time quests.

    0013 added ``is_one_time`` with a False default, so rows written earlier
    sit outside the partial unique index. Only the earliest completed row per
    quest is flagged: any historical duplicate stays unflagged instead of
    making the backfill (or the index) fail.
    """
    QuestCompletion = apps.get_model('quests', 'QuestCompletion')
    first_ids = list(
        QuestCompletion.objects.filter(
            status='completed',
            quest__quest_type='one_time',
        ).values('quest_id').annotate(first_id=Min('id')).values_list(
            'first_id',
            flat=True,
        )
    )
    for start in range(0, len(first_ids), 1000):
        QuestCompletion.objects.filter(
            id__in=first_ids[start:start + 1000],
        ).update(is_one_time=True)


class Migration(migrations.Migration):

    dependencies = [
        ('quests', '0013_completion_one_time_guard'),
    ]

    operations = [
        migrations.RunPython(flag_one_time_completions, migrations.RunPython.noop),
    ]
