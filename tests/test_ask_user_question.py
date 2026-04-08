import json
from src.streaming_utils import make_function_call_response_sse


def test_make_function_call_response_sse():
    result = make_function_call_response_sse(
        response_id="resp_123_1",
        call_id="toolu_abc",
        name="AskUserQuestion",
        arguments='{"question": "Overwrite?"}',
    )
    assert "event: response.output_item.added" in result
    parsed_lines = [line for line in result.strip().split("\n") if line.startswith("data: ")]
    data = json.loads(parsed_lines[0].removeprefix("data: "))
    assert data["item"]["type"] == "function_call"
    assert data["item"]["name"] == "AskUserQuestion"
    assert data["item"]["call_id"] == "toolu_abc"


def test_make_function_call_response_sse_id_format():
    result = make_function_call_response_sse(
        response_id="resp_abc_2",
        call_id="toolu_xyz",
        name="AskUserQuestion",
        arguments="{}",
    )
    data = json.loads(result.split("data: ")[1].split("\n")[0])
    assert data["item"]["id"] == "fc_toolu_xyz"
    assert data["response_id"] == "resp_abc_2"
