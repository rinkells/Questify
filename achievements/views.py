from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated

from .models import Achievement, UserAchievement
from .serializers import AchievementSerializer


class AchievementListView(ListAPIView):
    queryset = Achievement.objects.all()
    serializer_class = AchievementSerializer
    permission_classes = (IsAuthenticated,)


class UnlockedAchievementListView(ListAPIView):
    serializer_class = AchievementSerializer
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        return Achievement.objects.filter(
            user_achievements__user=self.request.user,
        ).distinct()
