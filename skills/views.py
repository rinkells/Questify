from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import (
    DetailSerializer,
    SkillTreeNodeSerializer,
    SkillUnlockResponseSerializer,
)
from .services import (
    SkillNotFoundError,
    SkillParentLockedError,
    SkillRequirementsNotMetError,
    SkillService,
)


class SkillTreeView(APIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(responses=SkillTreeNodeSerializer(many=True))
    def get(self, request):
        return Response(SkillService.get_tree(request.user))


class SkillUnlockView(APIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        request=None,
        responses={
            200: SkillUnlockResponseSerializer,
            400: DetailSerializer,
            404: DetailSerializer,
            409: DetailSerializer,
        },
    )
    def post(self, request, skill_id):
        try:
            unlock = SkillService.unlock_skill(request.user, skill_id)
        except SkillNotFoundError:
            return Response(
                {'detail': 'Skill not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except SkillParentLockedError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except SkillRequirementsNotMetError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            SkillUnlockResponseSerializer({
                'id': unlock.user_skill.skill_id,
                'status': 'unlocked',
                'unlocked_at': unlock.user_skill.unlocked_at,
                'achievements_unlocked': unlock.achievements_unlocked,
            }).data,
        )
