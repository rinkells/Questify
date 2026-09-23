from django.contrib import admin

from .models import Character, Stat, StatType


@admin.register(Character)
class CharacterAdmin(admin.ModelAdmin):
	list_display = ('user', 'level', 'xp', 'xp_to_next_level')
	list_filter = ('level',)


@admin.register(StatType)
class StatTypeAdmin(admin.ModelAdmin):
	list_display = ('name', 'display_name', 'icon')
	list_filter = ('name',)


@admin.register(Stat)
class StatAdmin(admin.ModelAdmin):
	list_display = ('character', 'stat_type', 'value')
	list_filter = ('stat_type',)
