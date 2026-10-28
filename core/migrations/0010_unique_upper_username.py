from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Upper


def ensure_no_upper_variant_usernames(apps, schema_editor):
    """Stop with an actionable message instead of a raw IntegrityError.

    The LOWER() index from 0006 does not cover every name that UPPER() folds
    together: 'audıt' (dotless i) and 'audit' differ under LOWER() but not
    under UPPER(), which is what username__iexact compares. Concurrent
    registrations could therefore create both. As in 0006, renaming either
    account would change someone's login, so an operator decides first.
    """
    User = apps.get_model('auth', 'User')
    users = User.objects.using(schema_editor.connection.alias)
    duplicates = (
        users.annotate(normalized=Upper('username'))
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
            'Cannot make usernames unique under UPPER(): rename or merge '
            'these accounts (id, username) and re-run migrate: '
            + '; '.join(str(group) for group in groups)
        )


class Migration(migrations.Migration):
    """Make the database agree with username__iexact.

    Registration and login compare usernames with username__iexact, which
    PostgreSQL evaluates with UPPER(). The LOWER() index from 0006 stays, and
    this one makes every pair the serializer treats as duplicates a conflict
    in the database too, so a lost race is an IntegrityError (a 400) rather
    than a second account that login can no longer tell apart.
    """

    dependencies = [
        ('core', '0009_drop_unique_lower_email'),
    ]

    operations = [
        migrations.RunPython(
            ensure_no_upper_variant_usernames,
            migrations.RunPython.noop,
        ),
        migrations.RunSQL(
            sql=(
                'CREATE UNIQUE INDEX auth_user_username_upper_uniq '
                'ON auth_user (UPPER(username));'
            ),
            reverse_sql='DROP INDEX auth_user_username_upper_uniq;',
        ),
    ]
