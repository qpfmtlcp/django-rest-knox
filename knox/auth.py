try:
    from hmac import compare_digest
except ImportError:
    def compare_digest(a, b):
        return a == b

import binascii

from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from django.utils import timezone

from rest_framework import exceptions
from rest_framework.authentication import (
    BaseAuthentication,
    get_authorization_header
)

from knox.crypto import hash_token
from knox.models import AuthToken
from knox.settings import CONSTANTS, knox_settings
from knox.signals import token_expired

User = settings.AUTH_USER_MODEL


class TokenAuthentication(BaseAuthentication):
    '''
    This authentication scheme uses Knox AuthTokens for authentication.

    Similar to DRF's TokenAuthentication, it overrides a large amount of that
    authentication scheme to cope with the fact that Tokens are not stored
    in plaintext in the database

    If sucessful
    - `request.user` will be a django `User` instance
    - `request.auth` will be an `AuthToken` instance
    '''
    model = AuthToken

    def authenticate(self, request):
        auth = get_authorization_header(request).split()

        if not auth or auth[0].lower() != b'token':
            return None
        if len(auth) == 1:
            msg = _('Invalid token header. No credentials provided.')
            raise exceptions.AuthenticationFailed(msg)
        elif len(auth) > 2:
            msg = _('Invalid token header. '
                    'Token string should not contain spaces.')
            raise exceptions.AuthenticationFailed(msg)

        user, auth_token = self.authenticate_credentials(auth[1])
        return (user, auth_token)

    def authenticate_credentials(self, token):
        '''
        Due to the random nature of hashing a salted value, this must inspect
        each auth_token individually to find the correct one.

        Tokens that have expired will be deleted and skipped
        '''
        msg = _('Invalid token.')
        token = token.decode("utf-8")
        for auth_token in AuthToken.objects.filter(
                token_key=token[:CONSTANTS.TOKEN_KEY_LENGTH]):
            if self._cleanup_token(auth_token):
                continue

            try:
                digest = hash_token(token, auth_token.salt)
            except (TypeError, binascii.Error):
                raise exceptions.AuthenticationFailed(msg)
            if compare_digest(digest, auth_token.digest):
                if knox_settings.AUTO_REFRESH:
                    self.renew_token(auth_token)
                return self.validate_user(auth_token)
        raise exceptions.AuthenticationFailed(msg)

    def renew_token(self, auth_token):
        current_expiry = auth_token.expires
        new_expiry = timezone.now() + knox_settings.TOKEN_TTL
        auth_token.expires = new_expiry
        # Throttle refreshing of token to avoid db writes
        if (new_expiry - current_expiry).total_seconds() > knox_settings.MIN_REFRESH_INTERVAL:
            auth_token.save(update_fields=('expires',))

    def validate_user(self, auth_token):
        if not auth_token.user.is_active:
            raise exceptions.AuthenticationFailed(
                _('User inactive or deleted.'))
        return (auth_token.user, auth_token)

    def authenticate_header(self, request):
        return 'Token'

    def _cleanup_token(self, auth_token):
        for other_token in auth_token.user.auth_token_set.all():
            if other_token.digest != auth_token.digest and other_token.expires is not None:
                if other_token.expires < timezone.now():
                    other_token.delete()
                    username = other_token.user.username
                    token_expired.send(sender=self.__class__, username=username, source="other_token")
        if auth_token.expires is not None:
            if auth_token.expires < timezone.now():
                username = auth_token.user.username
                auth_token.delete()
                token_expired.send(sender=self.__class__, username=username, source="auth_token")
                return True
        return False

