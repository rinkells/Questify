from django.db import migrations
from django.db.models import F, OuterRef, Subquery, Sum


def grant_initial_balances(apps, schema_editor):
    """Turn the XP each character earned before the wallet into coins, once.

    Character.xp is what XPRewardHandler actually credited. The completion
    history cannot stand in for it: xp_awarded of completions older than
    quests migration 0005 is only an estimate. XP that quest rewards already
    paid out as coins is left out; such rows exist only if the wallet ran
    between migrations 0001 and 0002. Characters that already have an
    initial grant are skipped, so running this again changes nothing.
    """
    Character = apps.get_model('core', 'Character')
    WalletTransaction = apps.get_model('wallet', 'WalletTransaction')
    database = schema_editor.connection.alias
    transactions = WalletTransaction.objects.using(database)
    already_granted = transactions.filter(kind='initial_grant').values('user_id')
    already_paid = dict(
        transactions.filter(kind='quest_reward')
        .values('user_id')
        .annotate(total=Sum('amount'))
        .values_list('user_id', 'total')
    )
    characters = list(
        Character.objects.using(database)
        .select_for_update()
        .filter(xp__gt=0)
        .exclude(user_id__in=already_granted)
        .order_by('pk')
    )
    granted, grants = [], []
    for character in characters:
        amount = character.xp - already_paid.get(character.user_id, 0)
        if amount <= 0:
            continue
        character.coins += amount
        granted.append(character)
        grants.append(
            WalletTransaction(
                user_id=character.user_id,
                kind='initial_grant',
                amount=amount,
                balance_after=character.coins,
            ),
        )
    Character.objects.using(database).bulk_update(
        granted,
        ['coins'],
        batch_size=500,
    )
    transactions.bulk_create(grants, batch_size=500)


def refuse_to_revoke_a_used_wallet(apps, schema_editor):
    """Stop the rollback once the wallet did more than the grants.

    Reverting further drops the ledger (0001's reverse deletes the table)
    while Character.coins (core 0011) stays, so a later grant would pay the
    same XP again and nothing would explain the balance. Once quests paid
    coins or rewards were redeemed, stop instead.
    """
    WalletTransaction = apps.get_model('wallet', 'WalletTransaction')
    used = WalletTransaction.objects.using(
        schema_editor.connection.alias,
    ).exclude(kind='initial_grant').count()
    if used:
        raise RuntimeError(
            f'Cannot revert the wallet after it has been used ({used} '
            'transactions besides initial grants): their ledger would be '
            'lost while Character.coins stays. Restore a backup taken before '
            'wallet migration 0002 instead.'
        )


def revoke_initial_balances(apps, schema_editor):
    """Take the grants back, but only while they are all the wallet did.

    With grants only, each balance is exactly its grant, so subtracting it
    cannot go negative.
    """
    refuse_to_revoke_a_used_wallet(apps, schema_editor)
    Character = apps.get_model('core', 'Character')
    WalletTransaction = apps.get_model('wallet', 'WalletTransaction')
    database = schema_editor.connection.alias
    grants = WalletTransaction.objects.using(database).filter(
        kind='initial_grant',
    )
    Character.objects.using(database).filter(
        user_id__in=grants.values('user_id'),
    ).update(
        coins=F('coins') - Subquery(
            grants.filter(user_id=OuterRef('user_id')).values('amount')[:1],
        ),
    )
    grants.delete()


class Migration(migrations.Migration):

    # Also run before a rollback starts (core.rollback_guards), so one that
    # goes past this migration stops before the later ones are reverted.
    rollback_guards = (refuse_to_revoke_a_used_wallet,)

    dependencies = [
        ('core', '0011_character_coins'),
        ('wallet', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(grant_initial_balances, revoke_initial_balances),
    ]
