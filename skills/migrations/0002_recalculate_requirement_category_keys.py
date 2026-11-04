from collections import defaultdict

from django.db import migrations

# The rules as of this migration, not quests.models.category_key: a later change to
# the function must not change what running this one means.
APOSTROPHES = str.maketrans({'’': "'", 'ʼ': "'"})


def normalized(name):
    return name.strip().lower().translate(APOSTROPHES)


def previous_key(name):
    return name.strip().lower()


def _store_keys(apps, schema_editor, key_of):
    SkillRequirement = apps.get_model('skills', 'SkillRequirement')
    requirements = SkillRequirement.objects.using(schema_editor.connection.alias)
    for pk, name in requirements.values_list('pk', 'category_name'):
        requirements.filter(pk=pk).exclude(category_key=key_of(name)).update(
            category_key=key_of(name),
        )


def use_normalized_keys(apps, schema_editor):
    """Stop with an actionable message rather than fail on the constraint at commit.

    A requirement is what gates a skill, so which of two to keep is an operator's
    decision, as for the categories in quests/0016.
    """
    SkillRequirement = apps.get_model('skills', 'SkillRequirement')
    requirements = SkillRequirement.objects.using(schema_editor.connection.alias)
    groups = defaultdict(list)
    for pk, skill, requirement_type, name in requirements.order_by('pk').values_list(
        'pk',
        'skill__name',
        'requirement_type',
        'category_name',
    ):
        groups[skill, requirement_type, normalized(name)].append((pk, name))
    duplicates = [
        f'skill {skill!r}, {requirement_type}: {rows}'
        for (skill, requirement_type, _), rows in groups.items()
        if len(rows) > 1
    ]
    if duplicates:
        raise RuntimeError(
            'Cannot recalculate the category keys of skill requirements: these '
            'differ only in an apostrophe. Rename or remove them (id, category '
            'name) and re-run migrate: ' + '; '.join(duplicates)
        )
    _store_keys(apps, schema_editor, normalized)


def use_previous_keys(apps, schema_editor):
    # The earlier rule tells more names apart, so this cannot produce a duplicate.
    _store_keys(apps, schema_editor, previous_key)


class Migration(migrations.Migration):
    """Bring the stored keys in line with category_key, which now merges apostrophes."""

    dependencies = [
        ('skills', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(use_normalized_keys, use_previous_keys),
    ]
