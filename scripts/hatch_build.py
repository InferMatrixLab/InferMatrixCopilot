"""Generate packaged plugin metadata without changing the source checkout."""
import json
from pathlib import Path
import re
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        plugin = root / "plugins/infermatrix-copilot/.codex-plugin/plugin.json"
        if not plugin.is_file():
            raise ValueError("host plugin metadata is required for a Copilot wheel")
        source = (root / "src/infermatrix_copilot/__init__.py").read_text("utf-8")
        package_version = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', source, re.M)
        if package_version is None:
            raise ValueError("missing distribution version")
        metadata = json.loads(plugin.read_text("utf-8"))
        metadata["version"] = package_version.group(1)
        self._temporary = tempfile.TemporaryDirectory(prefix="copilot-wheel-")
        generated = Path(self._temporary.name) / "plugin.json"
        generated.write_text(json.dumps(metadata, indent=2) + "\n", "utf-8")
        build_data.setdefault("force_include", {})[str(generated)] = (
            "infermatrix_copilot/_runtime/plugin.json")

    def finalize(self, version, build_data, artifact_path):
        temporary = getattr(self, "_temporary", None)
        if temporary is not None:
            temporary.cleanup()
