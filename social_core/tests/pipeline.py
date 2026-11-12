from social_core.pipeline.partial import partial


@partial
def ask_for_password(strategy, *args, **kwargs):
    if strategy.session_get("password"):
        return {"password": strategy.session_get("password")}
    return strategy.redirect(strategy.build_absolute_uri("/password"))


@partial
def ask_for_slug(strategy, *args, **kwargs):
    if strategy.session_get("slug"):
        return {"slug": strategy.session_get("slug")}
    return strategy.redirect(strategy.build_absolute_uri("/slug"))


def set_password(strategy, user, *args, **kwargs) -> None:
    user.set_password(kwargs["password"])


def set_slug(strategy, user, *args, **kwargs) -> None:
    user.slug = kwargs["slug"]


def remove_user(strategy, user, *args, **kwargs):
    return {"user": None}


@partial
def set_user_from_kwargs(strategy, *args, **kwargs):
    if strategy.session_get("attribute"):
        assert kwargs["user"].id is not None
        return None
    return strategy.redirect(strategy.build_absolute_uri("/attribute"))


@partial
def set_user_from_args(strategy, user, *args, **kwargs):
    if strategy.session_get("attribute"):
        assert user.id is not None
        return None
    return strategy.redirect(strategy.build_absolute_uri("/attribute"))


def return_third_party_fields(strategy, *args, **kwargs):
    """Custom step returning fields fetched from a third-party JSON payload.

    The payload happens to use names owned by the framework context; the
    pipeline runner must not let them replace the real objects.
    """
    return {
        "backend": "third-party-backend",
        "strategy": "third-party-strategy",
        "request": "third-party-request",
        "pipeline_index": 99,
        "user": kwargs.get("user"),
        "details": {**kwargs.get("details", {}), "third_party": "value"},
        "third_party_field": "third-party-value",
    }


def record_pipeline_call(strategy, backend, *args, **kwargs):
    """Record the framework context and pipeline data this step received."""
    strategy.pipeline_calls.append(
        {"backend": backend, "strategy": strategy, "kwargs": kwargs}
    )


@partial
def pause_until_attribute(strategy, *args, **kwargs):
    if strategy.session_get("attribute"):
        return {"resumed": True}
    return strategy.redirect(strategy.build_absolute_uri("/attribute"))
