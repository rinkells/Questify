"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from core.views import CharacterView
from achievements.views import AchievementListView, UnlockedAchievementListView
from quests.views import QuestCategoryViewSet, QuestViewSet


router = DefaultRouter()
router.register('quests', QuestViewSet, basename='quest')
router.register('quest-categories', QuestCategoryViewSet, basename='quest-category')

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include(router.urls)),
    path('api/character/', CharacterView.as_view(), name='character'),
    path(
        'api/achievements/',
        AchievementListView.as_view(),
        name='achievement-list',
    ),
    path(
        'api/achievements/unlocked/',
        UnlockedAchievementListView.as_view(),
        name='achievement-unlocked-list',
    ),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path(
        'api/docs/',
        SpectacularSwaggerView.as_view(url_name='schema'),
        name='swagger-ui',
    ),
]
