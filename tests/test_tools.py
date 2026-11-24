from did_ai_agent.tools.registry import READ_TOOLS, TOOLS, WRITE_TOOLS
from did_ai_agent.tools.schema import tool_schemas


def test_tool_names_are_unique():
    names = [t.name for t in TOOLS.values()]
    assert len(names) == len(set(names))


def test_read_and_write_are_separated():
    assert all(t.kind == "read" for t in READ_TOOLS)
    assert all(t.kind == "write" for t in WRITE_TOOLS)
    assert {t.name for t in WRITE_TOOLS} == {
        "propose_sign_message",
        "propose_issue_credential",
        "propose_publish_did",
    }


def test_every_tool_has_object_schema():
    for tool in TOOLS.values():
        assert tool.params["type"] == "object"
        assert isinstance(tool.params.get("properties"), dict)


def test_schemas_are_function_calling_shaped():
    schemas = tool_schemas()
    assert len(schemas) == len(TOOLS)
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["name"] in TOOLS
        assert schema["function"]["parameters"]["type"] == "object"


def test_no_tool_exposes_secret_like_parameter():
    banned = {"private_key", "privatekey", "mnemonic", "secret"}
    for tool in TOOLS.values():
        assert not (set(tool.params["properties"]) & banned), tool.name
