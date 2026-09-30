from django.contrib import admin

from .models import Quest, QuestCategory, QuestCompletion, QuestStatReward, Streak


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
		'deadline',
	)
	list_filter = ('quest_type', 'is_active', 'category')


@admin.register(QuestStatReward)
class QuestStatRewardAdmin(admin.ModelAdmin):
	list_display = ('quest', 'stat_type', 'amount')
	list_filter = ('stat_type',)


@admin.register(QuestCompletion)
class QuestCompletionAdmin(admin.ModelAdmin):
	list_display = ('quest', 'completed_at', 'status')
	list_filter = ('status', 'completed_at')


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
