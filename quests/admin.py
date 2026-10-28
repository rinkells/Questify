from django.contrib import admin

from .models import (
	DailyBonus,
	Quest,
	QuestCategory,
	QuestCompletion,
	QuestStatReward,
	Streak,
)


@admin.register(QuestCategory)
class QuestCategoryAdmin(admin.ModelAdmin):
	list_display = ('name', 'user', 'icon')
	list_filter = ('user',)


@admin.register(Quest)
class QuestAdmin(admin.ModelAdmin):
	list_display = (
		'title',
		'user',
		'category',
		'quest_type',
		'xp_reward',
		'is_active',
		'archived_at',
		'deadline',
	)
	list_filter = ('quest_type', 'is_active', 'category')
	readonly_fields = ('archived_at',)

	def has_delete_permission(self, request, obj=None):
		return False

	def save_model(self, request, obj, form, change):
		if obj.archived_at is not None:
			obj.is_active = False
		super().save_model(request, obj, form, change)


@admin.register(QuestStatReward)
class QuestStatRewardAdmin(admin.ModelAdmin):
	list_display = ('quest', 'stat_type', 'amount')
	list_filter = ('stat_type',)


@admin.register(QuestCompletion)
class QuestCompletionAdmin(admin.ModelAdmin):
	readonly_fields = (
		'user',
		'quest',
		'category_name_at_completion',
		'completed_at',
		'xp_awarded',
		'xp_awarded_is_estimate',
		'status',
	)
	list_display = (
		'user',
		'quest',
		'category_name_at_completion',
		'completed_at',
		'status',
		'xp_awarded',
		'xp_awarded_is_estimate',
	)
	list_filter = ('status', 'completed_at')

	def has_add_permission(self, request):
		return False

	def has_change_permission(self, request, obj=None):
		return False

	def has_delete_permission(self, request, obj=None):
		return False


@admin.register(DailyBonus)
class DailyBonusAdmin(admin.ModelAdmin):
	"""View-only: a bonus is paid together with its XP and coins."""

	readonly_fields = (
		'user',
		'local_date',
		'awarded_at',
		'xp_awarded',
		'quest_completion',
	)
	list_display = ('user', 'local_date', 'awarded_at', 'xp_awarded')
	list_filter = ('local_date',)

	def has_add_permission(self, request):
		return False

	def has_change_permission(self, request, obj=None):
		return False

	def has_delete_permission(self, request, obj=None):
		return False


@admin.register(Streak)
class StreakAdmin(admin.ModelAdmin):
	list_display = (
		'user',
		'quest_category',
		'current_length',
		'longest_length',
		'freezes_available',
		'last_completed_date',
	)
	list_filter = ('quest_category', 'last_completed_date')
