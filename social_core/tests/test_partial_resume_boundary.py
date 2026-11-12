from __future__ import annotations

import unittest
from typing import Any

from social_core.actions import do_complete
from social_core.backends.base import BaseAuth
from social_core.pipeline.partial import partial
from social_core.tests.models import (
    TestPartial,
    TestStorage,
    TestUserSocialAuth,
    User,
)
from social_core.tests.strategy import TestStrategy
from social_core.utils import PARTIAL_TOKEN_SESSION_NAME

RESUME_PIPELINE = (
    "social_core.pipeline.social_auth.social_details",
    "social_core.pipeline.social_auth.social_names",
    "social_core.pipeline.social_auth.social_uid",
    "social_core.pipeline.social_auth.auth_allowed",
    "social_core.pipeline.social_auth.social_user",
    "social_core.pipeline.user.get_username",
    "social_core.tests.test_partial_resume_boundary.ask_for_extra",
    "social_core.pipeline.user.create_user",
    "social_core.pipeline.social_auth.associate_user",
    "social_core.pipeline.social_auth.load_extra_data",
    "social_core.pipeline.user.user_details",
)


@partial
def ask_for_extra(strategy, *args, **kwargs):
    """Pause the login until the supplementary profile data is supplied."""
    if strategy.session_get("extra_profile"):
        return {"extra_profile": strategy.session_get("extra_profile")}
    return strategy.redirect(strategy.build_absolute_uri("/extra"))


class FixedBackendStrategy(TestStrategy):
    """Strategy that resumes the pipeline on the backend holding the flow."""

    def authenticate(self, backend, *args, **kwargs):
        kwargs["strategy"] = self
        kwargs["storage"] = self.storage
        kwargs["backend"] = backend
        args, kwargs = self.clean_authenticate_args(*args, **kwargs)
        return backend.authenticate(*args, **kwargs)


class FakeProvider(BaseAuth):
    """Provider-free backend that drives the real pipeline with fixed data."""

    name = "fake-provider"
    ID_KEY = "id"

    def __init__(self, strategy, response: dict[str, Any] | None = None) -> None:
        super().__init__(strategy)
        self._provider_response = response or {
            "id": "provider-uid-1",
            "username": "alice",
            "email": "alice@example.com",
        }

    def auth_url(self):
        return "/auth"

    def get_user_details(self, response):
        return {
            "username": response["username"],
            "email": response.get("email"),
        }

    def auth_complete(self, *args, **kwargs):
        return self.strategy.authenticate(
            self, response=self._provider_response, *args, **kwargs
        )


class SecondFakeProvider(FakeProvider):
    name = "fake-provider-two"


class TransientStorageError(Exception):
    """A recoverable storage outage, as a database or cache backend would raise."""


class FlakyPartial(TestPartial):
    cache: dict[str, FlakyPartial] = {}
    fail_loads_left = 0
    fail_destroys_left = 0
    fail_stores_left = 0

    @classmethod
    def reset_cache(cls) -> None:
        cls.fail_loads_left = 0
        cls.fail_destroys_left = 0
        cls.fail_stores_left = 0
        cls.cache = {}

    def save(self) -> None:
        if type(self).fail_stores_left:
            type(self).fail_stores_left -= 1
            raise TransientStorageError("temporary partial store failure")
        type(self).cache[self.token] = self

    @classmethod
    def load(cls, token):
        if cls.fail_loads_left:
            cls.fail_loads_left -= 1
            raise TransientStorageError("temporary partial load failure")
        return cls.cache.get(token)

    @classmethod
    def destroy(cls, token) -> None:
        if cls.fail_destroys_left:
            cls.fail_destroys_left -= 1
            raise TransientStorageError("temporary partial destroy failure")
        cls.cache.pop(token, None)


class FlakyStorage(TestStorage):
    partial = FlakyPartial

    @classmethod
    def is_integrity_error(cls, exception) -> bool:
        return isinstance(exception, TransientStorageError)


class ResumeBoundaryTestBase(unittest.TestCase):
    storage_class = TestStorage

    def setUp(self) -> None:
        super().setUp()
        for model in (User, TestUserSocialAuth, FlakyPartial):
            model.reset_cache()
        self.addCleanup(User.reset_cache)
        self.addCleanup(TestUserSocialAuth.reset_cache)
        self.addCleanup(FlakyPartial.reset_cache)
        self.logins: list[tuple[str, str]] = []

    def new_browser(self):
        strategy = FixedBackendStrategy(self.storage_class)
        strategy.set_settings(
            {
                "SOCIAL_AUTH_PIPELINE": RESUME_PIPELINE,
                "SOCIAL_AUTH_LOGIN_REDIRECT_URL": "/success",
                "SOCIAL_AUTH_LOGIN_ERROR_URL": "/error",
            }
        )
        return strategy

    def backend(self, strategy, *, backend_class=FakeProvider, response=None):
        return backend_class(strategy, response)

    def login(self, backend, user, social_user) -> None:
        self.logins.append((social_user.provider, social_user.uid))

    def pause(self, strategy, backend_class=FakeProvider, response=None):
        backend = self.backend(strategy, backend_class=backend_class, response=response)
        redirect = do_complete(backend, login=self.login)
        token = strategy.session_get(PARTIAL_TOKEN_SESSION_NAME)
        self.assertEqual(redirect.url, strategy.build_absolute_uri("/extra"))
        self.assertIsInstance(token, str)
        stored = self.storage_class.partial.load(token)
        assert stored is not None
        self.assertEqual(stored.backend, backend.name)
        self.assertEqual(stored.pipeline_type, "authentication")
        return backend, token, stored

    def resume(self, strategy, backend):
        return do_complete(backend, login=self.login)

    def attack_with_token(
        self, token, *, backend_class=FakeProvider, request_data=None
    ):
        """Present a token from a browser that never started that flow."""
        attacker = self.new_browser()
        backend = self.backend(attacker, backend_class=backend_class)
        attacker.set_request_data(
            {"partial_token": token, **(request_data or {})}, backend
        )
        redirect = do_complete(backend, login=self.login)
        return attacker, backend, redirect

    def assert_no_identity_created(self) -> None:
        self.assertEqual(self.logins, [])
        self.assertEqual(TestUserSocialAuth.cache_by_uid, {})
        self.assertEqual(User.cache, {})


class PartialResumeBoundaryTest(ResumeBoundaryTestBase):
    def test_normal_fill_and_continue_completes_the_login_once(self) -> None:
        browser = self.new_browser()
        backend, token, stored = self.pause(browser)

        # The partial keeps the provider identity captured before the pause.
        self.assertEqual(stored.kwargs["uid"], "provider-uid-1")
        self.assertEqual(stored.kwargs["details"]["username"], "alice")

        # The user fills the supplementary form and continues in the same browser.
        browser.session_set("extra_profile", "filled")
        redirect = self.resume(browser, backend)

        self.assertEqual(redirect.url, "/success")
        self.assertEqual(self.logins, [("fake-provider", "provider-uid-1")])
        social = TestUserSocialAuth.get_social_auth("fake-provider", "provider-uid-1")
        assert social is not None
        self.assertEqual(len(User.cache), 1)
        self.assertIsNone(self.storage_class.partial.load(token))
        self.assertIsNone(browser.session_get(PARTIAL_TOKEN_SESSION_NAME))

    def test_another_browser_cannot_resume_with_a_stolen_token(self) -> None:
        victim = self.new_browser()
        _, token, victim_partial = self.pause(victim)
        saved_kwargs = victim_partial.kwargs.copy()

        attacker, _, redirect = self.attack_with_token(token)

        self.assertEqual(redirect.url, "/error")
        self.assertIsNone(attacker.session_get(PARTIAL_TOKEN_SESSION_NAME))
        self.assert_no_identity_created()
        # The failed attempt neither consumes nor mutates the victim's flow.
        remaining = self.storage_class.partial.load(token)
        assert remaining is not None
        self.assertEqual(remaining.kwargs, saved_kwargs)

        victim.session_set("extra_profile", "filled")
        completed = self.resume(victim, self.backend(victim))
        self.assertEqual(completed.url, "/success")
        self.assertEqual(self.logins, [("fake-provider", "provider-uid-1")])

    def test_token_from_a_different_backend_is_rejected(self) -> None:
        victim = self.new_browser()
        _, token, _ = self.pause(victim, backend_class=FakeProvider)

        _, _, redirect = self.attack_with_token(
            token, backend_class=SecondFakeProvider
        )

        self.assertEqual(redirect.url, "/error")
        self.assert_no_identity_created()
        self.assertIsNotNone(self.storage_class.partial.load(token))

        victim.session_set("extra_profile", "filled")
        completed = self.resume(victim, self.backend(victim))
        self.assertEqual(completed.url, "/success")
        self.assertEqual(self.logins, [("fake-provider", "provider-uid-1")])

    def test_callback_with_a_different_provider_uid_is_rejected(self) -> None:
        browser = self.new_browser()
        _, token, _ = self.pause(browser)

        # The owning browser replays the token alongside new provider data that
        # identifies a different account.
        backend = self.backend(browser)
        browser.set_request_data(
            {"partial_token": token, "id": "provider-uid-2"}, backend
        )
        redirect = do_complete(backend, login=self.login)

        self.assertEqual(redirect.url, "/error")
        self.assertIsNone(self.storage_class.partial.load(token))
        self.assertIsNone(browser.session_get(PARTIAL_TOKEN_SESSION_NAME))
        self.assert_no_identity_created()

    def test_a_consumed_token_cannot_be_replayed(self) -> None:
        browser = self.new_browser()
        backend, token, _ = self.pause(browser)
        browser.session_set("extra_profile", "filled")
        first = self.resume(browser, backend)
        self.assertEqual(first.url, "/success")

        # The token is destroyed on success; presenting it again must not
        # establish a second association or a second login.
        replay_backend = self.backend(browser)
        browser.set_request_data({"partial_token": token}, replay_backend)
        replay = do_complete(replay_backend, login=self.login)

        self.assertEqual(replay.url, "/error")
        self.assertEqual(self.logins, [("fake-provider", "provider-uid-1")])
        self.assertEqual(len(TestUserSocialAuth.cache_by_uid), 1)

    def test_interleaved_flows_keep_their_identities_independent(self) -> None:
        first_browser = self.new_browser()
        second_browser = self.new_browser()

        first_backend, first_token, first_partial = self.pause(
            first_browser,
            response={"id": "uid-a", "username": "amy", "email": "amy@example.com"},
        )
        second_backend, second_token, second_partial = self.pause(
            second_browser,
            response={"id": "uid-b", "username": "ben", "email": "ben@example.com"},
        )
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(first_partial.kwargs["uid"], "uid-a")
        self.assertEqual(second_partial.kwargs["uid"], "uid-b")

        # Cross-fire both tokens from the wrong browsers while both flows pause.
        first_attacker, _, first_attack = self.attack_with_token(second_token)
        second_attacker, _, second_attack = self.attack_with_token(first_token)
        self.assertEqual(first_attack.url, "/error")
        self.assertEqual(second_attack.url, "/error")
        self.assert_no_identity_created()
        self.assertIsNotNone(self.storage_class.partial.load(first_token))
        self.assertIsNotNone(self.storage_class.partial.load(second_token))
        self.assertIsNone(first_attacker.session_get(PARTIAL_TOKEN_SESSION_NAME))
        self.assertIsNone(second_attacker.session_get(PARTIAL_TOKEN_SESSION_NAME))

        # Each legitimate owner resumes and gets exactly its own association.
        first_browser.session_set("extra_profile", "amy-form")
        second_browser.session_set("extra_profile", "ben-form")
        first_done = self.resume(first_browser, first_backend)
        second_done = self.resume(second_browser, second_backend)

        self.assertEqual(first_done.url, "/success")
        self.assertEqual(second_done.url, "/success")
        self.assertEqual(
            sorted(self.logins),
            [("fake-provider", "uid-a"), ("fake-provider", "uid-b")],
        )
        self.assertEqual(
            sorted(TestUserSocialAuth.cache_by_uid),
            [("fake-provider", "uid-a"), ("fake-provider", "uid-b")],
        )
        self.assertIsNone(self.storage_class.partial.load(first_token))
        self.assertIsNone(self.storage_class.partial.load(second_token))


class TransientStorageErrorResumeTest(ResumeBoundaryTestBase):
    storage_class = FlakyStorage

    def test_load_outage_does_not_consume_the_flow_and_retry_succeeds(self) -> None:
        browser = self.new_browser()
        backend, token, _ = self.pause(browser)
        browser.session_set("extra_profile", "filled")
        FlakyPartial.fail_loads_left = 1

        with self.assertRaises(TransientStorageError):
            self.resume(browser, backend)

        # The stored partial and session token stay usable for a later retry;
        # no partial pipeline step ran, so no user or association was created.
        self.assertIsNotNone(FlakyPartial.load(token))
        self.assertEqual(browser.session_get(PARTIAL_TOKEN_SESSION_NAME), token)
        self.assert_no_identity_created()

        redirect = self.resume(browser, backend)
        self.assertEqual(redirect.url, "/success")
        self.assertEqual(self.logins, [("fake-provider", "provider-uid-1")])
        self.assertEqual(len(TestUserSocialAuth.cache_by_uid), 1)

    def test_destroy_outage_keeps_resume_retryable_with_one_association(self) -> None:
        browser = self.new_browser()
        backend, token, _ = self.pause(browser)
        browser.session_set("extra_profile", "filled")
        FlakyPartial.fail_destroys_left = 1

        # The pipeline establishes the association before clean_partial_pipeline
        # destroys the partial, so the outage surfaces after that write but
        # before do_complete finalizes the login.
        with self.assertRaises(TransientStorageError):
            self.resume(browser, backend)
        self.assertEqual(self.logins, [])
        self.assertIsNotNone(FlakyPartial.load(token))
        social = TestUserSocialAuth.get_social_auth(
            "fake-provider", "provider-uid-1"
        )
        self.assertIsNotNone(social)

        # Retrying replays the same steps; social_user finds the existing
        # association instead of creating a second one, and login completes once.
        redirect = self.resume(browser, backend)
        self.assertEqual(redirect.url, "/success")
        self.assertEqual(self.logins, [("fake-provider", "provider-uid-1")])
        self.assertEqual(len(TestUserSocialAuth.cache_by_uid), 1)
        self.assertIsNone(FlakyPartial.load(token))

    def test_store_outage_on_pause_leaves_no_resume_state(self) -> None:
        browser = self.new_browser()
        FlakyPartial.fail_stores_left = 1

        with self.assertRaises(TransientStorageError):
            do_complete(self.backend(browser), login=self.login)

        # The session token is only written after a successful store, so a
        # failed pause cannot be resumed and no identity was established.
        self.assertIsNone(browser.session_get(PARTIAL_TOKEN_SESSION_NAME))
        self.assertEqual(FlakyPartial.cache, {})
        self.assert_no_identity_created()
