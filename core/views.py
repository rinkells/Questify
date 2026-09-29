from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import IsAuthenticated

from .serializers import CharacterSerializer


class CharacterView(RetrieveAPIView):
	serializer_class = CharacterSerializer
	permission_classes = (IsAuthenticated,)

	def get_object(self):
		return self.request.user.character
