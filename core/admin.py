from django.contrib import admin
from rest_framework.authtoken.models import TokenProxy

from .models import Character, Stat, StatType

# The legacy plaintext DRF tokens are no longer accepted for authentication.
# The app stays installed only because migration 0005 reads its table.
if admin.site.is_registered(TokenProxy):
	admin.site.unregister(TokenProxy)


@admin.register(Character)
class CharacterAdmin(admin.ModelAdmin):
	list_display = ('user', 'level', 'xp', 'xp_to_next_level', 'coins', 'timezone')
	list_filter = ('level',)

	# View only. Every field here has a rule that an admin edit would skip:
	# xp without recomputing the level (which never goes down), coins without
	# a wallet transaction, a timezone change without the 24-hour wait and
	# the streak realignment the API applies. A full save would also write
	# back xp and coins read when the POST began, undoing a completion or a
	# redemption committed meanwhile. Users set their timezone in the API.
	def has_add_permission(self, request):
		# Created with the user (core.signals), recreated if ever missing.
		return False

	def has_change_permission(self, request, obj=None):
		return False

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
