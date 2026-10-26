from django.db import migrations, models


def revoke_tokens_without_auth_hash(apps, schema_editor):
    """Existing tokens carry no password binding, so force one re-login."""
    HashedAPIToken = apps.get_model('core', 'HashedAPIToken')
    HashedAPIToken.objects.using(schema_editor.connection.alias).all().delete()


class Migration(migrations.Migration):
    """Bind API tokens to the owner's password state.

    Without this, a password change or reset left every previously issued
    token valid until it expired.
    """

    dependencies = [
        ('core', '0007_unique_lower_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='hashedapitoken',
            name='auth_hash',
            field=models.CharField(default='', editable=False, max_length=64),
        ),
        migrations.RunPython(
            revoke_tokens_without_auth_hash,
            migrations.RunPython.noop,
        ),
    ]
