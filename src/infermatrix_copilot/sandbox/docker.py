"""Docker adapter: reuse the original durable container lifecycle exactly once."""
from copy import copy
from dataclasses import dataclass, field, replace
import json
from pathlib import Path

from .base import SandboxExecution, refuse_reserved
from .context import validate_context, verify_context
from ..persistence import immutable_write_bytes
from ..source_fingerprint import package_tree_hash


@dataclass(frozen=True)
class DockerSandboxExecution(SandboxExecution):
    supports_credentials = True
    execution: object
    image: str
    credentials: object = field(default=None, repr=False)
    expected_context: object = field(default=None, repr=False)

    def _prepared_context(self):
        credentials = None
        if self.credentials is not None:
            credentials = {**self.credentials.metadata, **self.credentials.identity}
        return validate_context({'release': package_tree_hash(Path(__file__).resolve().parents[1]),
                                 'artifact': self.image, 'credentials': credentials})

    def with_context(self, expected):
        return replace(self, expected_context=verify_context(self._prepared_context(), expected))

    def with_credentials(self, credentials):
        if self.credentials is not None:
            raise ValueError('sandbox already has a native credential source')
        selected = replace(self, credentials=credentials)
        selected.preflight()
        return selected

    def context(self):
        binding = self.execution._binding()
        self.execution.get_result()  # validates retained worker proof before attestation
        value = json.loads((self.execution.root / 'execution-context.json').read_text())
        context = validate_context(value)
        if (context['artifact'] != binding['image']
                or (context['credentials'] or {}).get('snapshot_sha256') != binding.get('worker_credentials_sha256')):
            raise ValueError('sandbox context differs from original worker')
        return context

    def preflight(self, *, for_dispatch=True):
        from ..providers.container_execution import ContainerExecution, validate_container_image
        from ..providers.worker_credentials import WorkerCredentials

        if not isinstance(self.execution, ContainerExecution):
            raise ValueError('Docker sandbox requires the durable container executor')
        validate_container_image(self.image)
        if for_dispatch:
            refuse_reserved(self.execution.root / 'binding.json')
            self.execution._proxy_identity()
        if self.credentials is not None and not isinstance(self.credentials, WorkerCredentials):
            raise ValueError('unsupported sandbox credential source')

    def supported_input_protocol(self):
        return self.execution.supported_input_protocol(self.image)

    def require_inputs(self, value):
        self.execution.require_inputs(self.image, value)

    def execute(self, cmd, **kwargs):
        refuse_reserved(self.execution.root / 'binding.json')
        self.require_inputs(kwargs.get('frozen_inputs'))
        context = self._prepared_context()
        if self.expected_context is not None:
            verify_context(context, self.expected_context)
        self.execution.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        immutable_write_bytes(self.execution.root / 'execution-context.json',
                              json.dumps(context, sort_keys=True).encode())
        if self.credentials is None:
            return self.execution.run_command(cmd, image=self.image, **kwargs)
        # Channels and bind-mount paths are prepared on the execution host.
        with self.credentials.serve() as socket:
            execution = copy(self.execution)
            execution.credential_socket = str(socket)
            execution.credentials_sha256 = self.credentials.sha256
            return execution.run_command(cmd, image=self.image, **kwargs)

    def result(self, cmd, **kwargs):
        return self.execution.get_command_result(cmd, image=self.image, **kwargs)

    def read_result(self, *, recover=False):
        return self.execution.get_original_command_result(image=self.image, recover=recover)

    def cancel(self):
        return self.execution.cancel()
