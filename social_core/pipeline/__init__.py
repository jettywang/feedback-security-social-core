"""Authentication and disconnect pipelines.

A pipeline step returns either a dictionary of pipeline data, a framework
response object to halt the pipeline, or a falsy value to continue without
changes. Dictionaries are merged into the pipeline data passed to the
following steps, so steps may add or update extension data such as ``user``
and ``details`` or any custom names of their own.

The names in ``PIPELINE_FRAMEWORK_KEYS`` belong to the framework context that
the pipeline runner manages. Step output cannot replace them: entries under
those names are discarded instead of being merged. Custom steps, including
steps returning fields fetched from third-party services, must use other
names for their own data.
"""

# Names owned by the pipeline runner's framework context. ``strategy`` and
# ``backend`` are the objects the pipeline is running with, ``request`` is
# deliberately not exposed to pipeline data, and ``pipeline_index`` tracks
# the executing step.
PIPELINE_FRAMEWORK_KEYS: frozenset[str] = frozenset(
    {"strategy", "backend", "request", "pipeline_index"}
)

DEFAULT_AUTH_PIPELINE = (
    # Get the information we can about the user and return it in a simple
    # format to create the user instance later. On some cases the details are
    # already part of the auth response from the provider, but sometimes this
    # could hit a provider API.
    "social_core.pipeline.social_auth.social_details",
    # Populate missing full or component names from provider details.
    "social_core.pipeline.social_auth.social_names",
    # Get the social uid from whichever service we're authing thru. The uid is
    # the unique identifier of the given user in the provider.
    "social_core.pipeline.social_auth.social_uid",
    # Verifies that the current auth process is valid within the current
    # project, this is where emails and domains whitelists are applied (if
    # defined).
    "social_core.pipeline.social_auth.auth_allowed",
    # Checks if the current social-account is already associated in the site.
    "social_core.pipeline.social_auth.social_user",
    # Make up a username for this person, appends a random string at the end if
    # there's any collision.
    "social_core.pipeline.user.get_username",
    # Send a validation email to the user to verify its email address.
    # 'social_core.pipeline.mail.mail_validation',
    # Associates the current social details with another user account with
    # a similar email address.
    # 'social_core.pipeline.social_auth.associate_by_email',
    # Create a user account if we haven't found one yet.
    "social_core.pipeline.user.create_user",
    # Create the record that associated the social account with this user.
    "social_core.pipeline.social_auth.associate_user",
    # Populate the extra_data field in the social record with the values
    # specified by settings (and the default ones like access_token, etc).
    "social_core.pipeline.social_auth.load_extra_data",
    # Update the user record with any changed info from the auth service.
    "social_core.pipeline.user.user_details",
)

DEFAULT_DISCONNECT_PIPELINE = (
    # Verifies that the social association can be disconnected from the current
    # user (ensure that the user login mechanism is not compromised by this
    # disconnection).
    "social_core.pipeline.disconnect.allowed_to_disconnect",
    # Collects the social associations to disconnect.
    "social_core.pipeline.disconnect.get_entries",
    # Revoke any access_token when possible.
    "social_core.pipeline.disconnect.revoke_tokens",
    # Removes the social associations.
    "social_core.pipeline.disconnect.disconnect",
)
