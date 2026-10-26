from django.db import migrations


class Migration(migrations.Migration):
    """Drop the unique email index added in 0007.

    Email is not used for login or recovery, so uniqueness protected nothing,
    while the "email already exists" answer let anyone test whether an address
    has an account. Reintroduce uniqueness together with email verification.
    """

    dependencies = [
        ('core', '0008_apitoken_auth_hash'),
    ]

    operations = [
        migrations.RunSQL(
            sql='DROP INDEX IF EXISTS auth_user_email_lower_uniq;',
            reverse_sql=(
                'CREATE UNIQUE INDEX IF NOT EXISTS auth_user_email_lower_uniq '
                "ON auth_user (LOWER(email)) WHERE email <> '';"
            ),
        ),
    ]
