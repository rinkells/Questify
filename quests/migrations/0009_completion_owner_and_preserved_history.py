from django.conf import settings
from django.db import migrations, models
from django.db.models import OuterRef, Subquery
import django.db.models.deletion


def backfill_completion_owners(apps, schema_editor):
	Quest = apps.get_model('quests', 'Quest')
	QuestCompletion = apps.get_model('quests', 'QuestCompletion')
	database = schema_editor.connection.alias

	owner_id = (
		Quest.objects.using(database)
		.filter(pk=OuterRef('quest_id'))
		.values('user_id')[:1]
	)
	QuestCompletion.objects.using(database).filter(user__isnull=True).update(
		user_id=Subquery(owner_id),
	)


class Migration(migrations.Migration):

	dependencies = [
		('quests', '0008_alter_queststatreward_amount_and_more'),
		migrations.swappable_dependency(settings.AUTH_USER_MODEL),
	]

	operations = [
		migrations.AddField(
			model_name='questcompletion',
			name='user',
			field=models.ForeignKey(
				editable=False,
				null=True,
				on_delete=django.db.models.deletion.CASCADE,
				related_name='quest_completions',
				to=settings.AUTH_USER_MODEL,
			),
		),
		migrations.RunPython(
			backfill_completion_owners,
			migrations.RunPython.noop,
		),
		migrations.AlterField(
			model_name='questcompletion',
			name='user',
			field=models.ForeignKey(
				editable=False,
				on_delete=django.db.models.deletion.CASCADE,
				related_name='quest_completions',
				to=settings.AUTH_USER_MODEL,
			),
		),
		migrations.AlterField(
			model_name='questcompletion',
			name='quest',
			field=models.ForeignKey(
				blank=True,
				null=True,
				on_delete=django.db.models.deletion.SET_NULL,
				related_name='completions',
				to='quests.quest',
			),
		),
		migrations.AlterField(
			model_name='questcompletion',
			name='category_name_at_completion',
			field=models.CharField(
				blank=True,
				default=None,
				editable=False,
			max_length=100,
				null=True,
			),
		),
	]
