"""Nonsecret provider options in a trusted, durable execution profile."""
from copy import copy
from functools import cached_property


def option_fields(adapter):
    return {'model_mismatch_policy', *getattr(adapter, 'completion_worker_settings', ())}


def normalize_options(provider, value):
    from .registry import _construct_transport
    from ..config import Settings
    if not isinstance(value, dict) or set(value) - option_fields(_construct_transport(None, provider)):
        raise ValueError('unsupported native provider option fields')
    try:
        # BaseSettings merges dictionary fields from environment sources even
        # when an empty init dictionary is supplied. Validate with the same
        # compiled Settings rules without re-entering environment loading.
        validated = Settings.model_construct()
        Settings.__pydantic_validator__.validate_python(
            {'strict_backend': provider, 'native_completion_profiles': {},
             'native_session_profiles': {}, **value}, self_instance=validated)
    except (ValueError, TypeError):
        raise ValueError('invalid native provider options') from None
    return {key: getattr(validated, key) for key in value}


def settings_for_config(settings, config):
    options = getattr(config, 'provider_options', None) or {}
    if not options or all(getattr(settings, key, None) == value for key, value in options.items()):
        return settings
    if callable(getattr(settings, 'model_copy', None)):
        return settings.model_copy(deep=True, update=options)
    selected = copy(settings)
    for key, value in options.items():
        setattr(selected, key, value)
    return selected


def configured_settings(settings, provider):
    from .completion_profiles import completion_profile_config
    return settings_for_config(settings, completion_profile_config(settings, provider))


def bind_transport(transport, settings, config):
    """Reset settings-derived caches on the per-attempt copy, never its caller."""
    selected = copy(transport)
    original = transport.settings if transport.settings is not None else settings
    selected.settings = settings_for_config(original, config)
    if selected.settings is not transport.settings:
        for cls in type(selected).__mro__:
            for name, field in vars(cls).items():
                if isinstance(field, cached_property):
                    selected.__dict__.pop(name, None)
    return selected
