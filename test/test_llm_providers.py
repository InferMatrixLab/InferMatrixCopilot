"""Anthropic/OpenAI backend selection and protocol translation."""

from types import SimpleNamespace

from infermatrix_copilot.config import Settings
from infermatrix_copilot.llm import LLM


def test_auto_selects_the_only_configured_provider():
    anthropic = Settings(_env_file=None, anthropic_api_key="a")
    openai = Settings(_env_file=None, openai_api_key="o")

    assert anthropic.resolved_llm_provider == "anthropic"
    assert anthropic.shared_model == "claude-sonnet-5"
    assert openai.resolved_llm_provider == "openai"
    assert openai.shared_model == "gpt-5.6"


def test_explicit_provider_selects_matching_key_and_base_url():
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        anthropic_api_key="a",
        openai_api_key="o",
        openai_base_url="https://gateway.example/v1",
    )

    target = settings.tier_target("eco")
    assert (target.provider, target.api_key, target.base_url, target.host) == (
        "openai", "o", "https://gateway.example/v1", "gateway.example")


def test_openai_tool_calls_round_trip_through_internal_protocol():
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="resp_1",
            model="gpt-5.6",
            choices=[SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="checking",
                    tool_calls=[SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments='{"path":"a.py"}',
                        ),
                    )],
                ),
            )],
            usage=SimpleNamespace(
                prompt_tokens=12,
                completion_tokens=3,
                prompt_tokens_details=SimpleNamespace(cached_tokens=4),
            ),
        )

    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key="o",
    )
    llm = object.__new__(LLM)
    llm.settings = settings
    llm._provider = "openai"
    llm._default_model = ""
    llm._endpoint_host = "api.openai.com"
    llm._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        ),
    )

    reply = llm.create(
        system="review carefully",
        messages=[
            {"role": "user", "content": "review"},
            {"role": "assistant", "content": [{
                "type": "tool_use",
                "id": "old_call",
                "name": "read_file",
                "input": {"path": "old.py"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": "old_call",
                "content": "contents",
            }]},
        ],
        tools=[{
            "name": "read_file",
            "description": "Read one file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }],
    )

    assert captured["messages"][0] == {
        "role": "system", "content": "review carefully"}
    assert captured["messages"][-1] == {
        "role": "tool", "tool_call_id": "old_call", "content": "contents"}
    assert captured["tools"][0]["function"]["parameters"]["required"] == ["path"]
    assert reply.stop_reason == "tool_use"
    assert reply.text == "checking"
    assert reply.tool_uses[0].input == {"path": "a.py"}
    assert reply.usage == {
        "input_tokens": 12,
        "input_tokens_include_cache": True,
        "output_tokens": 3,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 0,
    }
