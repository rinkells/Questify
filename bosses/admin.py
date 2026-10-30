from django.contrib import admin

from core.admin import ReadOnlyAdminMixin

from .models import Boss, BossDefeat, BossStep


class BossStepInline(ReadOnlyAdminMixin, admin.TabularInline):
    """View-only: steps change through the API, which checks the quest."""

    model = BossStep
    fields = ('quest', 'added_at', 'counted')
    readonly_fields = fields
    extra = 0


@admin.register(Boss)
class BossAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'archived_at', 'created_at')
    list_filter = ('archived_at',)
    readonly_fields = ('user', 'archived_at')
    inlines = (BossStepInline,)

    def has_add_permission(self, request):
        # Created through the API, which enforces the per-user limits.
        return False

    def has_delete_permission(self, request, obj=None):
        # Archived instead, as in the API, so defeats keep their boss.
        return False

    def save_model(self, request, obj, form, change):
        # Only what the form edits: a full save would write back archived_at
        # as read when the POST began and restore a boss archived meanwhile.
        fields = set(form.changed_data)
        if fields:
            fields.add('updated_at')
            obj.save(update_fields=fields)


@admin.register(BossDefeat)
class BossDefeatAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """View-only: a defeat is recorded together with its XP and coins."""

    readonly_fields = (
        'boss',
        'user',
        'defeated_at',
        'xp_awarded',
        'quest_completion',
    )
    list_display = ('boss', 'user', 'defeated_at', 'xp_awarded')
    list_filter = ('defeated_at',)
