"""Application registration of backend configuration; locations stay independent."""
from abc import ABC, abstractmethod


class SandboxBackendConfig(ABC):
    native_cli: str

    @abstractmethod
    def profile(self, root, *, credentials=None): ...

    @abstractmethod
    def prepare(self, root, *, for_dispatch=True): ...


_CONFIGS = {}


def register_backend(kind, config_type):
    if (not isinstance(kind, str) or not kind or kind in _CONFIGS
            or not isinstance(config_type, type) or not issubclass(config_type, SandboxBackendConfig)
            or not callable(getattr(config_type, 'parse', None))):
        raise ValueError('invalid or duplicate sandbox backend registration')
    _CONFIGS[kind] = config_type


def parse_backend_config(value):
    if not isinstance(value, dict) or value.get('kind') not in _CONFIGS:
        raise ValueError('unsupported sandbox backend')
    return _CONFIGS[value['kind']].parse(value)
