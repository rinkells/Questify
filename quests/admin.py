from django.contrib import admin

from .models import Quest, QuestCategory, QuestCompletion, QuestStatReward


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
