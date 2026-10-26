from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django.core.exceptions import ValidationError


@lru_cache(maxsize=1)
def _known_timezones():
	return frozenset(available_timezones())


# Names that resolve to the host machine's own zone rather than a fixed IANA zone.
_HOST_DEPENDENT = frozenset({'localtime'})


def validate_timezone_name(value):
	# ZoneInfo() also accepts directory names such as "America", which raise
	# IsADirectoryError, so only names from the tz database are allowed.
	if value not in _known_timezones() or value in _HOST_DEPENDENT:
		raise ValidationError('Enter a valid IANA time zone.')
	try:
		ZoneInfo(value)
	except (ValueError, ZoneInfoNotFoundError, OSError) as exc:
		raise ValidationError('Enter a valid IANA time zone.') from exc
