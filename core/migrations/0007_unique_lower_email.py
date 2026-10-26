from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower


def ensure_no_duplicate_emails(apps, schema_editor):
    """Stop with an actionable message instead of a raw IntegrityError."""
    User = apps.get_model('auth', 'User')
    users = User.objects.using(schema_editor.connection.alias).exclude(email='')
    duplicates = (
        users.annotate(normalized=Lower('email'))
        .values('normalized')
        .annotate(total=Count('id'))
        .filter(total__gt=1)
        .values_list('normalized', flat=True)
    )
    groups = [
        list(
            users.filter(email__iexact=email)
            .order_by('id')
            .values_list('id', 'username')
        )
        for email in duplicates
    ]
    if groups:
        raise RuntimeError(
            'Cannot make emails unique: these accounts (id, username) share an '
            'email address. Clear or change the duplicates and re-run migrate: '
            + '; '.join(str(group) for group in groups)
        )


class Migration(migrations.Migration):
    """Enforce one account per email (case-insensitive; blank emails exempt).

    The registration serializer checks email__iexact, but two concurrent
    requests with the same email can both pass that check.
    """

    dependencies = [
        ('core', '0006_unique_lower_username'),
    ]

    operations = [
        migrations.RunPython(
            ensure_no_duplicate_emails,
            migrations.RunPython.noop,
        ),
        migrations.RunSQL(
            sql=(
                'CREATE UNIQUE INDEX auth_user_email_lower_uniq '
                "ON auth_user (LOWER(email)) WHERE email <> '';"
            ),
            reverse_sql='DROP INDEX auth_user_email_lower_uniq;',
        ),
    ]
