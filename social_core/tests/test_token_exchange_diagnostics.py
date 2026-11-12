"""Token-endpoint failures stop before profile lookup, pipeline, and association.

Each failure is reproduced with mocked HTTP at the token endpoint and checked
for exception type, stage, flow state, and whether account association ran.
Diagnostic output is limited to the locating fields of ``public_metadata()``.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

import requests
import responses

from social_core.backends.oauth import BaseOAuth2
from social_core.exceptions import (
    AuthCredentialError,
    AuthProviderError,
    AuthResponseError,
    AuthSessionError,
)
from social_core.tests.models import TestNonce, TestStorage, TestUserSocialAuth, User
from social_core.tests.strategy import TestStrategy
from social_core.utils import PARTIAL_TOKEN_SESSION_NAME

TOKEN_URL = "https://provider.example/oauth/token"
USER_DATA_URL = "https://provider.example/user"
PROXY_ERROR_PAGE = "<html><body>proxy error</body></html>"


class DiagnosticOAuth2(BaseOAuth2):
    name = "diagnostic"
    AUTHORIZATION_URL = "https://provider.example/oauth/authorize"
    ACCESS_TOKEN_URL = TOKEN_URL

    def get_user_details(self, response):
        return {
            "username": response.get("username"),
            "email": response.get("email", ""),
        }

    def user_data(self, access_token: str, *args, **kwargs) -> dict[str, Any] | None:
        return self.get_json(
            USER_DATA_URL, headers={"Authorization": f"Bearer {access_token}"}
        )


class TokenExchangeDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        User.reset_cache()
        TestUserSocialAuth.reset_cache()
        TestNonce.reset_cache()
        self.strategy = TestStrategy(TestStorage)
        self.strategy.set_settings(
            {
                "SOCIAL_AUTH_KEY": "client-id",
                "SOCIAL_AUTH_SECRET": "client-secret",
                "SOCIAL_AUTH_AUTHENTICATION_BACKENDS": (
                    "social_core.tests.test_token_exchange_diagnostics."
                    "DiagnosticOAuth2",
                ),
            }
        )
        self.backend = DiagnosticOAuth2(
            self.strategy, redirect_uri="http://myapp.com/complete/diagnostic"
        )
        self.state = self.backend.get_or_create_state()
        self.strategy.set_request_data(
            {"code": "authorization-code", "state": self.state}, self.backend
        )

    def tearDown(self) -> None:
        User.reset_cache()
        TestUserSocialAuth.reset_cache()
        TestNonce.reset_cache()

    def assert_flow_stopped_before_association(self, error, user_data, authenticate):
        # Locating fields only: no provider payload, tokens, or user data.
        self.assertEqual(
            set(error.public_metadata()),
            {"error_code", "error_source", "error_stage", "error_recovery"},
        )
        self.assertEqual(error.stage, "token_exchange")
        # The flow stopped before profile lookup and the pipeline.
        user_data.assert_not_called()
        authenticate.assert_not_called()
        # No account was created or associated, and nothing was logged in.
        self.assertEqual(User.cache, {})
        self.assertEqual(TestUserSocialAuth.cache_by_uid, {})
        self.assertIsNone(self.strategy.session_get("username"))
        self.assertIsNone(self.strategy.session_get("social_auth_last_login_backend"))
        # The single-use callback state was already consumed at the callback
        # boundary, so the same callback request cannot be replayed.
        self.assertIsNone(
            self.strategy.session_get(self.backend.transaction_key(self.state))
        )
        self.assertIsNone(
            self.strategy.session_get(self.backend._transaction_index_key())
        )
        self.assertIsNone(TestNonce.get(self.backend._nonce_scope(), self.state))
        with self.assertRaises(AuthSessionError):
            self.backend.validate_state()
        # No partial pipeline was saved.
        self.assertIsNone(self.strategy.session_get(PARTIAL_TOKEN_SESSION_NAME))
        # Only the token endpoint was requested.
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(responses.calls[0].request.url, TOKEN_URL)
        self.assertEqual(responses.calls[0].request.method, "POST")

    @responses.activate
    def test_token_request_timeout_is_safe_to_retry(self) -> None:
        responses.post(TOKEN_URL, body=requests.ConnectTimeout())
        with (
            patch.object(self.backend, "user_data") as user_data,
            patch.object(self.strategy, "authenticate") as authenticate,
            self.assertRaises(AuthProviderError) as caught,
        ):
            self.backend.auth_complete()
        error = caught.exception
        self.assertIsInstance(error.__cause__, requests.ConnectTimeout)
        self.assertEqual(error.code, "timeout")
        self.assertEqual(error.source, "provider_response")
        self.assertEqual(error.recovery, "retry_later")
        self.assertIsNone(error.status_code)
        self.assertEqual(
            error.public_metadata(),
            {
                "error_code": "timeout",
                "error_source": "provider_response",
                "error_stage": "token_exchange",
                "error_recovery": "retry_later",
            },
        )
        self.assert_flow_stopped_before_association(error, user_data, authenticate)

    @responses.activate
    def test_invalid_grant_requires_login_restart(self) -> None:
        responses.post(
            TOKEN_URL,
            status=200,
            json={
                "error": "invalid_grant",
                "error_description": "authorization code expired",
            },
        )
        with (
            patch.object(self.backend, "user_data") as user_data,
            patch.object(self.strategy, "authenticate") as authenticate,
            self.assertRaises(AuthCredentialError) as caught,
        ):
            self.backend.auth_complete()
        error = caught.exception
        self.assertEqual(error.code, "authorization_code_rejected")
        self.assertEqual(error.source, "provider_response")
        self.assertEqual(error.recovery, "restart_login")
        self.assertEqual(error.provider_code, "invalid_grant")
        self.assertEqual(
            error.public_metadata(),
            {
                "error_code": "authorization_code_rejected",
                "error_source": "provider_response",
                "error_stage": "token_exchange",
                "error_recovery": "restart_login",
            },
        )
        # The provider description stays out of the diagnostic output.
        self.assertNotIn("authorization code expired", str(error))
        self.assertNotIn("authorization code expired", str(error.public_metadata()))
        self.assert_flow_stopped_before_association(error, user_data, authenticate)

    @responses.activate
    def test_non_json_proxy_error_page(self) -> None:
        for status, family, code, recovery in (
            (502, AuthProviderError, "unavailable", "retry_later"),
            (200, AuthResponseError, "malformed_response", "contact_administrator"),
        ):
            self.tearDown()
            self.setUp()
            responses.reset()
            responses.post(
                TOKEN_URL,
                status=status,
                body=PROXY_ERROR_PAGE,
                content_type="text/html",
            )
            with (
                self.subTest(status=status),
                patch.object(self.backend, "user_data") as user_data,
                patch.object(self.strategy, "authenticate") as authenticate,
                self.assertRaises(family) as caught,
            ):
                self.backend.auth_complete()
            error = caught.exception
            self.assertEqual(error.code, code)
            self.assertEqual(error.source, "provider_response")
            self.assertEqual(error.recovery, recovery)
            self.assertEqual(error.status_code, status if status >= 400 else None)
            self.assertNotIn("proxy error", str(error))
            self.assertNotIn("proxy error", str(error.public_metadata()))
            self.assert_flow_stopped_before_association(error, user_data, authenticate)

    @responses.activate
    def test_successful_exchange_associates_account(self) -> None:
        # Control case: the harness detects association when the flow completes.
        responses.post(
            TOKEN_URL,
            status=200,
            json={"access_token": "access-token", "token_type": "bearer"},
        )
        responses.get(
            USER_DATA_URL,
            status=200,
            json={"id": 1, "username": "foobar", "email": "foo@bar.com"},
        )
        user = self.backend.auth_complete()
        self.assertEqual(user.username, "foobar")
        self.assertEqual(set(TestUserSocialAuth.cache_by_uid), {("diagnostic", "1")})
        social = TestUserSocialAuth.cache_by_uid[("diagnostic", "1")]
        self.assertIs(social.user, user)
        self.assertEqual(user.social, [social])
        self.assertEqual(len(responses.calls), 2)


if __name__ == "__main__":
    unittest.main()
