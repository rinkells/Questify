from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django.core.exceptions import ValidationError


@lru_cache(maxsize=1)
def _known_timezones():
	return frozenset(available_timezones())


def validate_timezone_name(value):
	# ZoneInfo() also accepts directory names such as "America", which raise
	# IsADirectoryError, so only names from the tz database are allowed.
	if value not in _known_timezones():
		raise ValidationError('Enter a valid IANA time zone.')
	try:
		ZoneInfo(value)
	except (ValueError, ZoneInfoNotFoundError, OSError) as exc:
		raise ValidationError('Enter a valid IANA time zone.') from exc
