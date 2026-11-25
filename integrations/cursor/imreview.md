Review the supplied target with InferMatrixCopilot Direct mode. If omitted, use
the current PR or working tree. Pass through any requested review options.

Call `get_workflow(skill_name="imreview", skill_protocol=1)` on the connected
InferMatrixCopilot MCP server. Follow the returned workflow, resolving its
references from the returned skill path. If the server lacks the workflow tool,
use the imreview skill installed with that server's release.
