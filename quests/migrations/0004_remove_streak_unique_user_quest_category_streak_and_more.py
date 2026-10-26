# Historical migration; verified with Django 5.2.18 on 2026-10-23 10:47

from datetime import date

from django.conf import settings
from django.db import migrations, models
from django.db.models import Count


MAX_POSITIVE_INTEGER = 2_147_483_647


def merge_duplicate_general_streaks(apps, schema_editor):
    Streak = apps.get_model('quests', 'Streak')
    database = schema_editor.connection.alias
    duplicate_user_ids = (
        Streak.objects.using(database)
        .filter(quest_category__isnull=True)
        .values('user_id')
        .annotate(row_count=Count('pk'))
        .filter(row_count__gt=1)
        .values_list('user_id', flat=True)
        .iterator()
    )

    for user_id in duplicate_user_ids:
        rows = list(
            Streak.objects.using(database).filter(
                user_id=user_id,
                quest_category__isnull=True,
            ),
        )
        latest = max(
            rows,
            key=lambda row: (row.last_completed_date or date.min, row.pk),
        )
        keeper = rows[0]
        keeper.current_length = latest.current_length
        keeper.last_completed_date = latest.last_completed_date
        keeper.longest_length = max(row.longest_length for row in rows)
        keeper.freezes_available = min(
            sum(row.freezes_available for row in rows),
            MAX_POSITIVE_INTEGER,
        )
        keeper.save(
            using=database,
            update_fields=(
                'current_length',
                'last_completed_date',
                'longest_length',
                'freezes_available',
            ),
        )
        Streak.objects.using(database).filter(
            pk__in=[row.pk for row in rows if row.pk != keeper.pk],
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('quests', '0003_alter_quest_xp_reward'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='streak',
            name='unique_user_quest_category_streak',
        ),
        migrations.RunPython(
            merge_duplicate_general_streaks,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='streak',
            constraint=models.UniqueConstraint(fields=('user', 'quest_category'), name='unique_user_quest_category_streak', nulls_distinct=False),
        ),
    ]
