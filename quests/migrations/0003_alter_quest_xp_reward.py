# Historical migration; verified with Django 5.2.18 on 2026-10-23 10:17

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('quests', '0002_streak'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quest',
            name='xp_reward',
            field=models.PositiveIntegerField(validators=[django.core.validators.MaxValueValidator(1000000)]),
        ),
    ]
