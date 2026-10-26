from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower


def ensure_no_case_variant_usernames(apps, schema_editor):
    """Stop with an actionable message instead of a raw IntegrityError.

    Before this migration, concurrent registrations could create "Bob" and
    "bob". Renaming either account would silently change someone's login, so
    an operator has to decide which accounts to merge or rename first.
    """
    User = apps.get_model('auth', 'User')
    users = User.objects.using(schema_editor.connection.alias)
    duplicates = (
        users.annotate(normalized=Lower('username'))
        .values('normalized')
        .annotate(total=Count('id'))
        .filter(total__gt=1)
        .values_list('normalized', flat=True)
    )
    groups = [
        list(
            users.filter(username__iexact=name)
            .order_by('id')
            .values_list('id', 'username')
        )
        for name in duplicates
    ]
    if groups:
        raise RuntimeError(
            'Cannot make usernames case-insensitively unique: rename or merge '
            'these accounts (id, username) and re-run migrate: '
            + '; '.join(str(group) for group in groups)
        )


class Migration(migrations.Migration):
    """Make username uniqueness case-insensitive at the database level.

    The registration serializer checks username__iexact, but two concurrent
    requests ("Bob" and "bob") can both pass that check, and the built-in
    unique constraint on auth_user.username is case-sensitive.
    """

    dependencies = [
        ('core', '0005_hashedapitoken'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(
            ensure_no_case_variant_usernames,
            migrations.RunPython.noop,
        ),
        migrations.RunSQL(
            sql=(
                'CREATE UNIQUE INDEX auth_user_username_lower_uniq '
                'ON auth_user (LOWER(username));'
            ),
            reverse_sql='DROP INDEX auth_user_username_lower_uniq;',
        ),
    ]
