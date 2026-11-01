from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def api_exception_handler(exc, context):
    """Answer body-size violations with JSON instead of Django's HTML 400."""
    if isinstance(exc, RequestDataTooBig):
        return Response(
            {'detail': 'Request body too large.'},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    if isinstance(exc, TooManyFieldsSent):
        return Response(
            {'detail': 'Too many fields in the request.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return drf_exception_handler(exc, context)


def validation_error(exc):
    """The 400 the API answers for a DomainError: ``{field or 'detail': message}``."""
    return DRFValidationError({exc.field or 'detail': str(exc)})
