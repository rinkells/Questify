from django.conf import settings
from django.db import models


class Character(models.Model):
	user = models.OneToOneField(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='character',
	)
	level = models.PositiveIntegerField(default=1)
	xp = models.PositiveIntegerField(default=0)
	xp_to_next_level = models.PositiveIntegerField(default=100)

	def __str__(self):
		return f'{self.user} character'


class StatType(models.Model):
	name = models.CharField(max_length=10, unique=True)
	display_name = models.CharField(max_length=100)
	icon = models.CharField(max_length=100, blank=True)

	def __str__(self):
		return self.display_name


class Stat(models.Model):
	character = models.ForeignKey(
		Character,
		on_delete=models.CASCADE,
		related_name='stats',
	)
	stat_type = models.ForeignKey(
		StatType,
		on_delete=models.CASCADE,
		related_name='stats',
	)
	value = models.IntegerField(default=0)

	class Meta:
		unique_together = ('character', 'stat_type')

	def __str__(self):
		return f'{self.character}: {self.stat_type} ({self.value})'
