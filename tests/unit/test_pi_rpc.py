from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.ai.pi_rpc import (
    PiImageAttachment,
    PiRPCError,
    PiRPCOutputLimitError,
    PiRPCProcessError,
    PiRPCProtocolError,
    PiRPCRequest,
    PiRPCTimeoutError,
    run_pi_rpc,
)

FAKE_CHILD = r"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode = sys.argv[1]
marker = Path(sys.argv[2]) if len(sys.argv) > 2 else None
request = json.loads(sys.stdin.buffer.readline())
crlf = mode in {"crlf", "inspect"}


def emit(value):
    ending = b"\r\n" if crlf else b"\n"
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    sys.stdout.buffer.write(payload + ending)
    sys.stdout.buffer.flush()


def prompt_response():
    emit({"id": request["id"], "type": "response", "command": "prompt", "success": True})


def complete(text="assistant prose", duplicate=False):
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
    emit({
        "type": "tool_execution_update",
        "toolCallId": "call-1",
        "toolName": "codex_imagegen",
        "args": {},
        "partialResult": {},
    })
    tool_end = {
        "type": "tool_execution_end",
        "toolCallId": "call-1",
        "toolName": "codex_imagegen",
        "isError": False,
        "result": {"details": {"outputPath": "generated/authoritative.png", "status": "completed"}},
    }
    emit(tool_end)
    if duplicate:
        emit(tool_end)
    emit({
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })
    emit({"type": "agent_settled"})


if mode in {"success", "crlf", "inspect"}:
    expected_images = [{"type": "image", "data": "QUJD", "mimeType": "image/png"}]
    if mode == "inspect" and request.get("images") != expected_images:
        os._exit(11)
    prompt_response()
    complete("before\u2028after" if mode == "crlf" else "generated/assistant-only.png")
elif mode == "malformed":
    sys.stdout.buffer.write(b"{not-json\n")
    sys.stdout.buffer.flush()
elif mode == "invalid-utf8":
    sys.stdout.buffer.write(b"\xff\n")
    sys.stdout.buffer.flush()
elif mode == "mismatch":
    emit({"id": "wrong-command-id", "type": "response", "command": "prompt", "success": True})
elif mode == "duplicate-response":
    prompt_response()
    prompt_response()
elif mode == "duplicate-tool":
    prompt_response()
    complete(duplicate=True)
elif mode == "tool-before-prompt":
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
elif mode == "tool-update-missing":
    prompt_response()
    emit({"type": "tool_execution_update", "toolCallId": "call-1", "toolName": "codex_imagegen"})
elif mode == "tool-end-missing":
    prompt_response()
    emit({
        "type": "tool_execution_end",
        "toolCallId": "call-1",
        "toolName": "codex_imagegen",
        "isError": False,
        "result": {},
    })
elif mode == "duplicate-start":
    prompt_response()
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
elif mode == "update-name-mismatch":
    prompt_response()
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
    emit({"type": "tool_execution_update", "toolCallId": "call-1", "toolName": "other_tool"})
elif mode == "end-name-mismatch":
    prompt_response()
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
    emit({
        "type": "tool_execution_end",
        "toolCallId": "call-1",
        "toolName": "other_tool",
        "isError": False,
        "result": {},
    })
elif mode == "eof":
    prompt_response()
elif mode == "crash":
    prompt_response()
    os._exit(7)
elif mode == "timeout":
    prompt_response()
    time.sleep(30)
elif mode in {"confirm-yes", "confirm-no", "duplicate-confirm"}:
    prompt_response()
    emit({
        "type": "extension_ui_request",
        "id": "confirm-1",
        "method": "confirm",
        "title": "Confirm",
    })
    response = json.loads(sys.stdin.buffer.readline())
    if marker is not None:
        marker.write_text(str(response.get("confirmed")), encoding="utf-8")
    if mode == "duplicate-confirm":
        emit({
            "type": "extension_ui_request",
            "id": "confirm-2",
            "method": "confirm",
            "title": "Confirm again",
        })
    elif response.get("confirmed"):
        complete()
elif mode == "unexpected-confirm":
    prompt_response()
    emit({
        "type": "extension_ui_request",
        "id": "notify-1",
        "method": "notify",
        "message": "unexpected",
    })
elif mode == "late-confirm":
    prompt_response()
    emit({"type": "agent_settled"})
    emit({"type": "extension_ui_request", "id": "late-1", "method": "confirm"})
elif mode == "stdout-limit":
    sys.stdout.buffer.write(b"x" * 4096)
    sys.stdout.buffer.flush()
elif mode == "stderr-limit":
    sys.stderr.buffer.write(b"e" * 4096)
    sys.stderr.buffer.flush()
elif mode == "terminate":
    child = subprocess.Popen([
        sys.executable,
        "-c",
        "import pathlib,sys,time; time.sleep(0.3); pathlib.Path(sys.argv[1]).write_text('alive')",
        str(marker),
    ])
    if marker is not None:
        marker.with_suffix(".child-pid").write_text(str(child.pid), encoding="utf-8")
    prompt_response()
    time.sleep(30)
"""


@pytest.fixture
def fake_child(tmp_path: Path) -> Path:
    path = tmp_path / "fake_pi.py"
    path.write_text(FAKE_CHILD, encoding="utf-8")
    return path


def request_for(
    fake_child: Path,
    mode: str,
    *,
    timeout: float = 2.0,
    max_stdout_bytes: int = 64 * 1024,
    max_stderr_bytes: int = 64 * 1024,
    attachments: tuple[PiImageAttachment, ...] = (),
    authorize_confirmation: bool = False,
    marker: Path | None = None,
) -> PiRPCRequest:
    argv = [sys.executable, str(fake_child), mode]
    if marker is not None:
        argv.append(str(marker))
    return PiRPCRequest(
        argv=argv,
        cwd=fake_child.parent,
        prompt="Generate one image.\u2028Keep this content.",
        timeout=timeout,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        attachments=attachments,
        authorize_confirmation=authorize_confirmation,
    )


@pytest.mark.asyncio
async def test_success_collects_tool_details_and_keeps_assistant_prose_separate(
    fake_child: Path,
) -> None:
    result = await run_pi_rpc(request_for(fake_child, "success"))

    assert result.prompt_response["success"] is True
    assert (
        result.tool_completions[0]["result"]["details"]["outputPath"]
        == "generated/authoritative.png"
    )
    assert result.assistant_text == "generated/assistant-only.png"
    assert result.events[-1]["type"] == "agent_settled"
    assert result.confirmation_sent is False


@pytest.mark.asyncio
async def test_crlf_and_unicode_line_separators_are_protocol_safe(fake_child: Path) -> None:
    result = await run_pi_rpc(
        request_for(
            fake_child,
            "inspect",
            attachments=(PiImageAttachment(data="QUJD", mime_type="image/png"),),
        )
    )

    assert result.assistant_text == "generated/assistant-only.png"
    assert any(event["type"] == "tool_execution_end" for event in result.events)

    unicode_result = await run_pi_rpc(request_for(fake_child, "crlf"))
    assert unicode_result.assistant_text == "before\u2028after"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "error_type", "codes"),
    [
        ("malformed", PiRPCProtocolError, {"protocol_error"}),
        ("invalid-utf8", PiRPCProtocolError, {"protocol_error"}),
        ("mismatch", PiRPCProtocolError, {"id_mismatch"}),
        ("duplicate-response", PiRPCProtocolError, {"duplicate_response"}),
        ("duplicate-tool", PiRPCProtocolError, {"duplicate_tool_completion"}),
        ("eof", PiRPCProcessError, {"eof_before_settle"}),
        ("crash", PiRPCProcessError, {"eof_before_settle", "child_error"}),
    ],
)
async def test_protocol_and_child_failures_are_bounded(
    fake_child: Path,
    mode: str,
    error_type: type[PiRPCError],
    codes: set[str],
) -> None:
    with pytest.raises(error_type) as raised:
        await run_pi_rpc(request_for(fake_child, mode))

    assert raised.value.code in codes
    assert "not-json" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("tool-before-prompt", "tool_before_prompt_response"),
        ("tool-update-missing", "tool_start_missing"),
        ("tool-end-missing", "tool_start_missing"),
        ("duplicate-start", "duplicate_tool_start"),
        ("update-name-mismatch", "tool_name_mismatch"),
        ("end-name-mismatch", "tool_name_mismatch"),
    ],
)
async def test_tool_lifecycle_is_correlated_to_unique_starts(
    fake_child: Path,
    mode: str,
    code: str,
) -> None:
    with pytest.raises(PiRPCProtocolError) as raised:
        await run_pi_rpc(request_for(fake_child, mode))

    assert raised.value.code == code


@pytest.mark.asyncio
async def test_confirmation_is_correlated_and_answered_once(
    fake_child: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "confirmation.txt"
    result = await run_pi_rpc(
        request_for(
            fake_child,
            "confirm-yes",
            authorize_confirmation=True,
            marker=marker,
        )
    )

    assert result.confirmation_sent is True
    assert marker.read_text(encoding="utf-8") == "True"
    assert len(result.tool_completions) == 1


@pytest.mark.asyncio
async def test_denied_confirmation_gets_negative_response_and_fails(
    fake_child: Path,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "confirmation.txt"
    with pytest.raises(PiRPCProtocolError) as raised:
        await run_pi_rpc(request_for(fake_child, "confirm-no", marker=marker))

    assert raised.value.code == "confirmation_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "code", "authorized"),
    [
        ("duplicate-confirm", "confirmation_duplicate", True),
        ("unexpected-confirm", "unexpected_ui_request", True),
        ("late-confirm", "late_event", True),
    ],
)
async def test_duplicate_unexpected_and_late_confirmation_fail(
    fake_child: Path,
    mode: str,
    code: str,
    authorized: bool,
) -> None:
    with pytest.raises(PiRPCProtocolError) as raised:
        await run_pi_rpc(request_for(fake_child, mode, authorize_confirmation=authorized))

    assert raised.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "timeout"])
async def test_timeout_terminates_the_process_group(fake_child: Path, mode: str) -> None:
    with pytest.raises(PiRPCTimeoutError):
        await run_pi_rpc(request_for(fake_child, mode, timeout=0.1))


@pytest.mark.asyncio
async def test_timeout_kills_descendant_process(fake_child: Path, tmp_path: Path) -> None:
    marker = tmp_path / "timeout-descendant.txt"
    with pytest.raises(PiRPCTimeoutError):
        await run_pi_rpc(request_for(fake_child, "terminate", timeout=0.2, marker=marker))
    await asyncio.sleep(0.45)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_stdout_and_stderr_bounds_are_retained_only_bounded_in_failures(
    fake_child: Path,
) -> None:
    with pytest.raises(PiRPCOutputLimitError) as stdout_error:
        await run_pi_rpc(request_for(fake_child, "stdout-limit", max_stdout_bytes=128))
    assert len(stdout_error.value._stdout_tail) <= 128
    assert not hasattr(stdout_error.value, "stdout")

    with pytest.raises(PiRPCOutputLimitError) as stderr_error:
        await run_pi_rpc(request_for(fake_child, "stderr-limit", max_stderr_bytes=128))
    assert len(stderr_error.value._stderr_tail) <= 128
    assert not hasattr(stderr_error.value, "stderr")


@pytest.mark.asyncio
async def test_cancellation_reraises_and_kills_descendant_process(
    fake_child: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "descendant-alive.txt"
    task = asyncio.create_task(
        run_pi_rpc(request_for(fake_child, "terminate", timeout=10, marker=marker))
    )
    await asyncio.sleep(0.15)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.45)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_cancelled_error_is_not_converted_to_public_failure(fake_child: Path) -> None:
    task = asyncio.create_task(run_pi_rpc(request_for(fake_child, "timeout", timeout=10)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_no_process_is_left_after_spawn_failure(tmp_path: Path) -> None:
    request = PiRPCRequest(
        argv=[str(tmp_path / "does-not-exist")],
        cwd=tmp_path,
        prompt="hello",
        timeout=1,
        max_stdout_bytes=128,
        max_stderr_bytes=128,
    )
    with pytest.raises(PiRPCProcessError) as raised:
        await run_pi_rpc(request)
    assert raised.value.code == "spawn_error"


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (PiRPCRequest, ValueError),
    ],
)
def test_request_validation_is_explicit(value: type[PiRPCRequest], error: type[Exception]) -> None:
    with pytest.raises(error):
        value(
            argv=[],
            cwd=Path("."),
            prompt="hello",
            timeout=1,
            max_stdout_bytes=1,
            max_stderr_bytes=1,
        )
