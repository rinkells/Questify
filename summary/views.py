from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from core.serializers import DetailSerializer
from core.throttling import AtomicUserRateThrottle

from .selectors import MAX_MONTHS_BACK, MonthError, monthly_summary, parse_month
from .serializers import MonthlySummarySerializer


class SummaryRateThrottle(AtomicUserRateThrottle):
    """The general user throttle with a budget of its own (the 'summary' rate)."""

    scope = 'summary'


class MonthlySummaryView(APIView):
    permission_classes = (IsAuthenticated,)
    # Six aggregates per request, so a budget of its own on top of the
    # general throttles.
    throttle_classes = (
        *api_settings.DEFAULT_THROTTLE_CLASSES,
        SummaryRateThrottle,
    )

    @extend_schema(
        parameters=[
            OpenApiParameter(
                'month',
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                pattern=r'^[0-9]{4}-[0-9]{2}$',
                description=(
                    'YYYY-MM in your timezone; the current month when '
                    'omitted. From the month your wallet history begins, '
                    f'at most {MAX_MONTHS_BACK} months back.'
                ),
            ),
        ],
        responses={200: MonthlySummarySerializer, 400: DetailSerializer},
    )
    def get(self, request):
        value = request.query_params.get('month')
        try:
            month = None if value is None else parse_month(value)
            summary = monthly_summary(request.user, month)
        except MonthError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(MonthlySummarySerializer(summary).data)
