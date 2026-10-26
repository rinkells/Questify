# Generated for Questify domain data hardening.

from django.db import migrations, models
from django.db.models import CharField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce


def backfill_category_snapshots_and_reward_policy(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    QuestCompletion = apps.get_model('quests', 'QuestCompletion')
    database = schema_editor.connection.alias

    category_name = (
        Quest.objects.using(database)
        .filter(pk=OuterRef('quest_id'))
        .values('category__name')[:1]
    )
    QuestCompletion.objects.using(database).update(
        category_name_at_completion=Coalesce(
            Subquery(category_name),
            Value(''),
            output_field=CharField(max_length=100),
        ),
    )

    Quest.objects.using(database).filter(
        quest_type__in=('daily', 'habit'),
    ).update(xp_reward=25)
    Quest.objects.using(database).filter(
        quest_type='one_time',
    ).update(xp_reward=50)


class Migration(migrations.Migration):

    dependencies = [
        ('quests', '0006_questcompletion_qc_status_date_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='questcompletion',
            name='category_name_at_completion',
            field=models.CharField(
                blank=True,
                default='',
                editable=False,
                max_length=100,
            ),
        ),
        migrations.RunPython(
            backfill_category_snapshots_and_reward_policy,
            migrations.RunPython.noop,
        ),
    ]
