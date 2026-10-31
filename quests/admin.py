from django.contrib import admin, messages

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
	# Each of these has a rule that an admin edit would skip: the reward
	# follows the server policy for the type, the type is fixed once the quest
	# has a completion, a passed deadline cannot move and a new one must lie
	# in 2000-2100. The owner and the per-user quota are the API's too.
	readonly_fields = ('user', 'quest_type', 'xp_reward', 'deadline', 'archived_at')

	def has_add_permission(self, request):
		# Created through the API, which enforces the quota and the reward.
		return False

	def has_delete_permission(self, request, obj=None):
		return False

	def get_form(self, request, obj=None, **kwargs):
		form = super().get_form(request, obj, **kwargs)
		if obj is not None and 'category' in form.base_fields:
			# Categories belong to users: only the owner's can be chosen.
			form.base_fields['category'].queryset = QuestCategory.objects.filter(
				user_id=obj.user_id,
			)
		return form

	def save_model(self, request, obj, form, change):
		# Lock the row and write only what the form edits. A full save would
		# put back archived_at as read when the POST began, and so restore a
		# quest its owner archived in the meantime.
		archived_at = Quest.objects.select_for_update().values_list(
			'archived_at',
			flat=True,
		).get(pk=obj.pk)
		fields = set(form.changed_data)
		if archived_at is not None and 'is_active' in fields:
			# An archived quest stays inactive (a database constraint).
			obj.is_active = False
		if 'category' in fields and obj.category_id is not None:
			# Locked like the API does, so a category deleted meanwhile is
			# not written as a dangling reference.
			category_exists = QuestCategory.objects.select_for_update().filter(
				pk=obj.category_id,
			).values_list('pk', flat=True).first()
			if category_exists is None:
				fields.discard('category')
				self.message_user(
					request,
					'The category was deleted in the meantime; it was not changed.',
					messages.WARNING,
				)
		if fields:
			fields.add('updated_at')
			obj.save(update_fields=fields)


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
