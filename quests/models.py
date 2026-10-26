from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import StatType

MAX_XP_REWARD = 1_000
MAX_STAT_REWARD = 1_000_000


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
	xp_reward = models.PositiveIntegerField(
		validators=[MaxValueValidator(MAX_XP_REWARD)],
	)
	is_active = models.BooleanField(default=True)
	archived_at = models.DateTimeField(null=True, blank=True, editable=False)
	deadline = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		indexes = [
			models.Index(fields=('user', 'is_active')),
		]
		constraints = [
			models.CheckConstraint(
				condition=Q(xp_reward__lte=MAX_XP_REWARD),
				name='quest_xp_reward_max_1000',
			),
			models.CheckConstraint(
				condition=Q(archived_at__isnull=True) | Q(is_active=False),
				name='archived_quest_must_be_inactive',
			),
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
	amount = models.PositiveIntegerField(
		default=0,
		validators=[MaxValueValidator(MAX_STAT_REWARD)],
	)

	class Meta:
		constraints = [
			models.CheckConstraint(
				condition=Q(amount__lte=MAX_STAT_REWARD),
				name='quest_stat_reward_max_1000000',
			),
		]

	def __str__(self):
		return f'{self.quest}: {self.stat_type} (+{self.amount})'


class QuestCompletion(models.Model):
	class CompletionStatus(models.TextChoices):
		COMPLETED = 'completed', 'Completed'
		FAILED = 'failed', 'Failed'

	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='quest_completions',
		editable=False,
	)
	quest = models.ForeignKey(
		Quest,
		on_delete=models.SET_NULL,
		related_name='completions',
		null=True,
		blank=True,
	)
	category_name_at_completion = models.CharField(
		max_length=100,
		blank=True,
		default=None,
		null=True,
		editable=False,
	)
	completed_at = models.DateTimeField(default=timezone.now, editable=False)
	xp_awarded = models.PositiveIntegerField(default=0)
	xp_awarded_is_estimate = models.BooleanField(default=False, editable=False)
	status = models.CharField(
		max_length=10,
		choices=CompletionStatus.choices,
	)

	class Meta:
		indexes = [
			models.Index(fields=('quest', 'completed_at')),
			models.Index(
				fields=('status', 'completed_at'),
				name='qc_status_date_idx',
			),
			models.Index(
				fields=('user', 'status', 'completed_at'),
				name='qc_user_status_date_idx',
			),
		]
		constraints = [
			models.CheckConstraint(
				condition=Q(xp_awarded__lte=MAX_XP_REWARD),
				name='completion_xp_awarded_max_1000',
			),
		]

	def save(self, *args, **kwargs):
		if self._state.adding and self.quest_id and (
			self.user_id is None
			or self.category_name_at_completion is None
		):
			quest_data = Quest.objects.filter(pk=self.quest_id).values(
				'user_id',
				'category__name',
			).first()
			if quest_data:
				if self.user_id is None:
					self.user_id = quest_data['user_id']
				if self.category_name_at_completion is None:
					self.category_name_at_completion = (
						quest_data['category__name'] or ''
					)
		if self.category_name_at_completion is None:
			self.category_name_at_completion = ''
		super().save(*args, **kwargs)

	def __str__(self):
		quest_title = self.quest.title if self.quest_id else 'Deleted quest'
		return f'{quest_title}: {self.status}'


class Streak(models.Model):
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='streaks',
	)
	quest_category = models.ForeignKey(
		QuestCategory,
		on_delete=models.CASCADE,
		related_name='streaks',
		null=True,
		blank=True,
	)
	current_length = models.PositiveIntegerField(default=0)
	longest_length = models.PositiveIntegerField(default=0)
	freezes_available = models.PositiveIntegerField(default=0)
	last_completed_date = models.DateField(null=True, blank=True)

	class Meta:
		constraints = [
			models.UniqueConstraint(
				fields=('user', 'quest_category'),
				name='unique_user_quest_category_streak',
				nulls_distinct=False,
			),
		]

	def __str__(self):
		category = self.quest_category or 'daily activity'
		return f'{self.user} - {category}: {self.current_length}'
