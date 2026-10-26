from django.contrib import admin
from rest_framework.authtoken.models import TokenProxy

from .models import Character, Stat, StatType

# The legacy plaintext DRF tokens are no longer accepted for authentication.
# The app stays installed only because migration 0005 reads its table.
if admin.site.is_registered(TokenProxy):
	admin.site.unregister(TokenProxy)


@admin.register(Character)
class CharacterAdmin(admin.ModelAdmin):
	list_display = ('user', 'level', 'xp', 'xp_to_next_level')
	list_filter = ('level',)

	def has_delete_permission(self, request, obj=None):
		# Every API call expects each user to own exactly one character.
		return False


@admin.register(StatType)
class StatTypeAdmin(admin.ModelAdmin):
	list_display = ('name', 'display_name', 'icon')
	list_filter = ('name',)


@admin.register(Stat)
class StatAdmin(admin.ModelAdmin):
	list_display = ('character', 'stat_type', 'value')
	list_filter = ('stat_type',)
