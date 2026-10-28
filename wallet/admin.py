from django.contrib import admin

from .models import Reward, RewardRedemption, WalletTransaction


class ReadOnlyAdmin(admin.ModelAdmin):
    """History rows: written only together with the balance they explain."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Reward)
class RewardAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'cost', 'is_active', 'archived_at')
    list_filter = ('is_active',)
    readonly_fields = ('archived_at',)

    def has_delete_permission(self, request, obj=None):
        # Archived instead, as in the API, so redemptions keep their reward.
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            super().save_model(request, obj, form, change)
            return
        # Lock the row and write only what the form edits. A full save would
        # put back archived_at and is_active as read when the POST began, and
        # so restore a reward its owner archived in the meantime.
        archived_at = Reward.objects.select_for_update().values_list(
            'archived_at',
            flat=True,
        ).get(pk=obj.pk)
        fields = set(form.changed_data)
        if archived_at is not None and 'is_active' in fields:
            # An archived reward stays inactive (a database constraint).
            obj.is_active = False
        if fields:
            fields.add('updated_at')
        obj.save(update_fields=fields)


@admin.register(RewardRedemption)
class RewardRedemptionAdmin(ReadOnlyAdmin):
    readonly_fields = (
        'user',
        'reward',
        'title_at_redemption',
        'cost_at_redemption',
        'redeemed_at',
    )
    list_display = readonly_fields
    list_filter = ('redeemed_at',)


@admin.register(WalletTransaction)
class WalletTransactionAdmin(ReadOnlyAdmin):
    readonly_fields = (
        'user',
        'kind',
        'amount',
        'balance_after',
        'quest_completion',
        'redemption',
        'daily_bonus',
        'created_at',
    )
    list_display = ('user', 'kind', 'amount', 'balance_after', 'created_at')
    list_filter = ('kind', 'created_at')
