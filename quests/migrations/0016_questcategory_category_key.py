from collections import defaultdict

from django.db import migrations, models

# The rule as of this migration, not quests.models.category_key: a later change to
# the function must not change what running this one means.
APOSTROPHES = str.maketrans({'’': "'", 'ʼ': "'"})


def normalized(name):
    return name.strip().lower().translate(APOSTROPHES)


def fill_category_keys(apps, schema_editor):
    """Fill the key from each name, which stays as it was typed.

    Stops with an actionable message instead of a raw IntegrityError. Quests and
    completions hang on a category, so which of two to rename or merge is an
    operator's decision, as for the usernames in core/0006.
    """
    QuestCategory = apps.get_model('quests', 'QuestCategory')
    categories = QuestCategory.objects.using(schema_editor.connection.alias)
    groups = defaultdict(list)
    for pk, user_id, name in categories.order_by('pk').values_list(
        'pk',
        'user_id',
        'name',
    ):
        groups[user_id, normalized(name)].append((pk, name))
    duplicates = [
        f'user {user_id}: {rows}'
        for (user_id, _), rows in groups.items()
        if len(rows) > 1
    ]
    if duplicates:
        raise RuntimeError(
            'Cannot make category names unique per user, ignoring case and '
            'apostrophes: rename or merge these categories (id, name) and re-run '
            'migrate: ' + '; '.join(duplicates)
        )
    for (_, key), rows in groups.items():
        categories.filter(pk=rows[0][0]).update(category_key=key)


class Migration(migrations.Migration):
    """Make category names unique per user at the database level."""

    dependencies = [
        ('quests', '0015_daily_bonus'),
    ]

    operations = [
        migrations.AddField(
            model_name='questcategory',
            name='category_key',
            field=models.CharField(
                blank=True,
                default='',
                editable=False,
                max_length=200,
            ),
        ),
        # Going back, the column goes with the constraint, and the names were never
        # changed, so there is nothing to undo.
        migrations.RunPython(fill_category_keys, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='questcategory',
            constraint=models.UniqueConstraint(
                fields=('user', 'category_key'),
                name='unique_user_category_key',
            ),
        ),
    ]
