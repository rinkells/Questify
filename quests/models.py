from django.conf import settings
from django.db import models

from core.models import StatType


class QuestCategory(models.Model):
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='quest_categories',
	)
	name = models.CharField(max_length=100)
	icon = models.CharField(max_length=100, blank=True, null=True)

	def __str__(self):
		return self.name


class Quest(models.Model):
	class QuestType(models.TextChoices):
		DAILY = 'daily', 'Daily'
		ONE_TIME = 'one_time', 'One time'
		HABIT = 'habit', 'Habit'

	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='quests',
	)
	category = models.ForeignKey(
		QuestCategory,
		on_delete=models.SET_NULL,
		related_name='quests',
		null=True,
		blank=True,
	)
	title = models.CharField(max_length=255)
	description = models.TextField(blank=True, null=True)
	quest_type = models.CharField(max_length=20, choices=QuestType.choices)
	xp_reward = models.PositiveIntegerField()
	is_active = models.BooleanField(default=True)
	deadline = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		indexes = [
			models.Index(fields=('user', 'is_active')),
		]

	def __str__(self):
		return self.title


class QuestStatReward(models.Model):
	quest = models.ForeignKey(
		Quest,
		on_delete=models.CASCADE,
		related_name='stat_rewards',
	)
	stat_type = models.ForeignKey(
		StatType,
		on_delete=models.CASCADE,
		related_name='quest_rewards',
	)
	amount = models.PositiveIntegerField(default=0)

	def __str__(self):
		return f'{self.quest}: {self.stat_type} (+{self.amount})'


class QuestCompletion(models.Model):
	class CompletionStatus(models.TextChoices):
		COMPLETED = 'completed', 'Completed'
		FAILED = 'failed', 'Failed'

	quest = models.ForeignKey(
		Quest,
		on_delete=models.CASCADE,
		related_name='completions',
	)
	completed_at = models.DateTimeField(auto_now_add=True)
	status = models.CharField(
		max_length=10,
		choices=CompletionStatus.choices,
	)

	class Meta:
		indexes = [
			models.Index(fields=('quest', 'completed_at')),
		]

	def __str__(self):
		return f'{self.quest}: {self.status}'
