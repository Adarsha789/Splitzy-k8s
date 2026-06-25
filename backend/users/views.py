from django.conf import settings as django_settings
from rest_framework.views import APIView
from rest_framework import generics, mixins
from django.contrib.auth import get_user_model, authenticate
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny


# third party import
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError

# userdefined imports
from .serializers import CustomUserSerializer, PasswordChangeSerializer, LoginSerializer


# user model
User = get_user_model()


# Create your views here.
class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        try:
            serializer = CustomUserSerializer(data=request.data)
            if serializer.is_valid(raise_exception=True):
                usr = serializer.save()
                refresh = RefreshToken.for_user(usr)
                response = Response(
                    {
                        "access": str(refresh.access_token),
                    },
                    status=status.HTTP_201_CREATED,
                )
                response.set_cookie(
                    key="refresh_token",
                    value=str(refresh),
                    httponly=True,
                    secure=django_settings.SESSION_COOKIE_SECURE,
                    samesite=django_settings.SESSION_COOKIE_SAMESITE,
                    path="/",
                    max_age=7 * 24 * 60 * 60,  # 7 days
                )
                return response
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            email = serializer.data.get("email")
            password = serializer.data.get("password")
            user = authenticate(email=email, password=password)
            if user is not None:
                refresh = RefreshToken.for_user(user)
                response = Response(
                    {
                        "access": str(refresh.access_token),
                    },
                    status=status.HTTP_200_OK,
                )
                response.set_cookie(
                    key="refresh_token",
                    value=str(refresh),
                    httponly=True,
                    secure=django_settings.SESSION_COOKIE_SECURE,
                    samesite=django_settings.SESSION_COOKIE_SAMESITE,
                    path="/",
                    max_age=7 * 24 * 60 * 60,  # 7 days
                )

                return response
            else:
                return Response(
                    {"detail": "Invalid Credentials"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )


class PasswordChangeView(APIView):
    def patch(self, request, *args, **kwargs):
        try:
            serializer = PasswordChangeSerializer(data=request.data)
            if serializer.is_valid(raise_exception=True):
                old_password = serializer.data.get("old_password")
                new_password = serializer.data.get("new_password")
                token = request.COOKIES.get("refresh_token")
                if not token:
                    return Response(
                        {"error": "Refresh token not found"},
                        status=status.HTTP_401_UNAUTHORIZED,
                    )
                token = RefreshToken(token)
                # RefreshToken takes base64_encoded_token_string , Parses it (encoded refresh token string) into a RefreshToken object

                user = request.user
                if user.check_password(old_password):
                    token.blacklist()  # first blacklisting the refresh token
                    user.set_password(new_password)
                    user.save()
                    return Response(
                        {"detail": "Password Reset Successfull!"},
                        status=status.HTTP_200_OK,
                    )
                return Response(
                    {"detail": "Old password Unmatched!"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)


class ProfileView(
    generics.GenericAPIView, mixins.RetrieveModelMixin, mixins.UpdateModelMixin
):
    queryset = User.objects.all()
    serializer_class = CustomUserSerializer

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)

    # make user detials editable
    def patch(self, request, *args, **kwargs):
        pass

    def get_object(self):
        return self.request.user


class RefreshTokenView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get("refresh_token")

        if not refresh_token:
            return Response(
                {"error": "Refresh token not found"},
                status=401,
            )
        try:
            refresh = RefreshToken(refresh_token)
            new_access = str(refresh.access_token)
            return Response({"access": new_access})
        except TokenError:
            return Response(
                {"error": "Invalid or expired refresh token"},
                status=401,
            )


class LogoutView(APIView):
    def post(self, request):
        response = Response({"detail": "Logged out successfully."})
        response.delete_cookie("refresh_token")
        return response


class CustomUsersListView(generics.GenericAPIView, mixins.ListModelMixin):
    queryset = User.objects.all()
    serializer_class = CustomUserSerializer

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)
