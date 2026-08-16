from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

import app.ai.pi_rpc as pi_rpc
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
    emit({"type": "turn_end"})
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled"})


def spawn_delayed_marker(*, ignore_term=False, keep_rpc_fd=False, delay=0.35):
    assert marker is not None
    held_fd = os.dup(sys.stdout.fileno()) if keep_rpc_fd else -1
    ready = marker.with_suffix(".ready")
    descendant_code = (
        "import os,pathlib,signal,sys,time;"
        "fd=int(sys.argv[1]);marker=pathlib.Path(sys.argv[2]);"
        "ready=pathlib.Path(sys.argv[3]);"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN) if sys.argv[4]=='1' else None;"
        "ready.write_text('ready');"
        "time.sleep(float(sys.argv[5]));"
        "marker.write_text('alive');"
        "os.close(fd) if fd>=0 else None"
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            descendant_code,
            str(held_fd),
            str(marker),
            str(ready),
            "1" if ignore_term else "0",
            str(delay),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(held_fd,) if held_fd >= 0 else (),
    )
    if held_fd >= 0:
        os.close(held_fd)
    marker.with_suffix(".descendant-pid").write_text(str(child.pid), encoding="utf-8")
    for _ in range(1000):
        if ready.exists():
            break
        time.sleep(0.001)
    else:
        os._exit(12)


def leader_exit_payload(final_payload, *, fill_queue=True):
    marker.with_suffix(".leader-pid").write_text(str(os.getpid()), encoding="utf-8")
    prompt = json.dumps(
        {"id": request["id"], "type": "response", "command": "prompt", "success": True},
        separators=(",", ":"),
    ).encode() + b"\n"
    filler = b"{}\n" * 1024 if fill_queue else b""
    sys.stdout.buffer.write(prompt + filler + final_payload)
    sys.stdout.buffer.flush()
    os._exit(0)


if mode in {"success", "crlf", "inspect"}:
    expected_images = [{"type": "image", "data": "QUJD", "mimeType": "image/png"}]
    if mode == "inspect" and request.get("images") != expected_images:
        os._exit(11)
    prompt_response()
    complete("before\u2028after" if mode == "crlf" else "generated/assistant-only.png")
elif mode == "leader-exit-success":
    spawn_delayed_marker()
    final = b'{"type":"agent_end"}\n{"type":"agent_settled"}\n'
    leader_exit_payload(final)
elif mode == "leader-exit-protocol":
    spawn_delayed_marker()
    leader_exit_payload(b"{not-json\n")
elif mode == "leader-exit-cancel":
    spawn_delayed_marker(ignore_term=True, keep_rpc_fd=True, delay=0.8)
    leader_exit_payload(b"", fill_queue=False)
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
elif mode == "second-start-before-end":
    prompt_response()
    if marker is not None:
        subprocess.Popen([
            sys.executable,
            "-c",
            "import pathlib as p,sys,time;time.sleep(.3);p.Path(sys.argv[1]).write_text('alive')",
            str(marker),
        ])
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
    emit({"type": "tool_execution_start", "toolCallId": "call-2", "toolName": "codex_imagegen"})
    time.sleep(30)
elif mode == "second-start-after-end":
    prompt_response()
    if marker is not None:
        subprocess.Popen([
            sys.executable,
            "-c",
            "import pathlib as p,sys,time;time.sleep(.3);p.Path(sys.argv[1]).write_text('alive')",
            str(marker),
        ])
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
    emit({
        "type": "tool_execution_end",
        "toolCallId": "call-1",
        "toolName": "codex_imagegen",
        "isError": False,
        "result": {"details": {"outputPath": "generated/authoritative.png", "status": "completed"}},
    })
    emit({"type": "tool_execution_start", "toolCallId": "call-2", "toolName": "codex_imagegen"})
    time.sleep(30)
elif mode == "open-tool-settled":
    prompt_response()
    emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "codex_imagegen"})
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled"})
elif mode == "settled-no-agent-end":
    prompt_response()
    emit({"type": "agent_settled"})
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
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled"})
    emit({"type": "extension_ui_request", "id": "late-1", "method": "confirm"})
elif mode == "dense-records":
    prompt_response()
    for _ in range(1000):
        emit({})
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled"})
elif mode == "valid-220":
    prompt_response()
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    for _ in range(220):
        emit({"type": "message_update"})
    emit({
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    })
    emit({"type": "turn_end"})
    emit({"type": "agent_end", "messages": [{"large": "payload"}], "willRetry": False})
    emit({"type": "agent_settled"})
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
    allowed_tool_names: tuple[str, ...] = ("codex_imagegen",),
    max_tool_starts: int = 1,
    max_jsonl_records: int = 4096,
    max_evidence_records: int = 64,
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
        allowed_tool_names=allowed_tool_names,
        max_tool_starts=max_tool_starts,
        max_jsonl_records=max_jsonl_records,
        max_evidence_records=max_evidence_records,
    )


async def wait_for_pid_file(path: Path, *, timeout: float = 1.0) -> int:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"PID file was not created: {path}")
        await asyncio.sleep(0.01)
    return int(path.read_text(encoding="utf-8"))


async def wait_for_process_exit(pid: int, *, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)


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
@pytest.mark.parametrize("mode", ["second-start-before-end", "second-start-after-end"])
async def test_second_unique_start_is_rejected_and_process_group_stops(
    fake_child: Path,
    tmp_path: Path,
    mode: str,
) -> None:
    marker = tmp_path / f"{mode}.txt"
    with pytest.raises(PiRPCProtocolError) as raised:
        await run_pi_rpc(request_for(fake_child, mode, marker=marker))

    assert raised.value.code == "tool_start_limit"
    await asyncio.sleep(0.45)
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("open-tool-settled", "tool_incomplete"),
        ("settled-no-agent-end", "agent_end_missing"),
    ],
)
async def test_settlement_requires_completed_tools_and_agent_run(
    fake_child: Path,
    mode: str,
    code: str,
) -> None:
    with pytest.raises(PiRPCProtocolError) as raised:
        await run_pi_rpc(request_for(fake_child, mode))

    assert raised.value.code == code


@pytest.mark.asyncio
async def test_tool_free_request_rejects_evaluator_tool_at_start(fake_child: Path) -> None:
    with pytest.raises(PiRPCProtocolError) as raised:
        await run_pi_rpc(
            request_for(
                fake_child,
                "success",
                allowed_tool_names=(),
                max_tool_starts=0,
            )
        )

    assert raised.value.code == "unexpected_tool"


@pytest.mark.asyncio
async def test_dense_record_stream_fails_at_record_bound(fake_child: Path) -> None:
    with pytest.raises(PiRPCProtocolError) as raised:
        await run_pi_rpc(
            request_for(
                fake_child,
                "dense-records",
                max_jsonl_records=16,
                max_evidence_records=8,
            )
        )

    assert raised.value.code == "record_limit"


@pytest.mark.asyncio
async def test_protocol_evidence_fails_at_record_bound(fake_child: Path) -> None:
    with pytest.raises(PiRPCProtocolError) as raised:
        await run_pi_rpc(
            request_for(
                fake_child,
                "success",
                max_jsonl_records=64,
                max_evidence_records=3,
            )
        )

    assert raised.value.code == "evidence_limit"


@pytest.mark.asyncio
async def test_valid_220_record_sequence_retains_only_bounded_evidence(
    fake_child: Path,
) -> None:
    result = await run_pi_rpc(
        request_for(
            fake_child,
            "valid-220",
            max_jsonl_records=256,
            max_evidence_records=8,
            allowed_tool_names=(),
            max_tool_starts=0,
        )
    )

    assert result.assistant_text == "done"
    assert result.events == (
        result.prompt_response,
        {"type": "agent_end"},
        {"type": "agent_settled"},
    )
    assert all(event["type"] != "message_update" for event in result.events)


@pytest.mark.asyncio
async def test_stdout_reader_applies_bounded_queue_backpressure() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b'{"type":"message_update"}\n' * 8)
    stream.feed_eof()
    queue: asyncio.Queue[pi_rpc._QueueItem] = asyncio.Queue(maxsize=2)
    capture = pi_rpc._ByteCapture(4096)
    reader = asyncio.create_task(pi_rpc._read_stdout(stream, queue, capture, 16))

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert queue.qsize() == 2
    assert not reader.done()

    items: list[pi_rpc._QueueItem] = []
    while not reader.done():
        items.append(await asyncio.wait_for(queue.get(), timeout=1))
        await asyncio.sleep(0)
    items.extend(queue.get_nowait() for _ in range(queue.qsize()))
    await reader

    assert sum(isinstance(item, pi_rpc._Record) for item in items) == 8
    assert any(isinstance(item, pi_rpc._StreamEnd) for item in items)


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
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("leader-exit-success", None),
        ("leader-exit-protocol", PiRPCProtocolError),
    ],
)
async def test_exited_leader_descendants_are_cleaned_without_delayed_markers(
    fake_child: Path,
    tmp_path: Path,
    mode: str,
    error_type: type[PiRPCError] | None,
) -> None:
    markers: list[Path] = []
    for iteration in range(10):
        marker = tmp_path / f"{mode}-{iteration}.txt"
        markers.append(marker)
        request = request_for(
            fake_child,
            mode,
            marker=marker,
            allowed_tool_names=(),
            max_tool_starts=0,
        )
        if error_type is None:
            result = await run_pi_rpc(request)
            assert result.events[-1]["type"] == "agent_settled"
        else:
            with pytest.raises(error_type) as raised:
                await run_pi_rpc(request)
            assert raised.value.code == "protocol_error"

    await asyncio.sleep(0.4)
    for marker in markers:
        assert not marker.exists()
        descendant_pid = int(marker.with_suffix(".descendant-pid").read_text(encoding="utf-8"))
        assert await wait_for_process_exit(descendant_pid)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
async def test_repeated_cancellation_cleans_descendant_after_leader_exit(
    fake_child: Path,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cancelled-leader-exit.txt"
    task = asyncio.create_task(
        run_pi_rpc(
            request_for(
                fake_child,
                "leader-exit-cancel",
                timeout=10,
                marker=marker,
                allowed_tool_names=(),
                max_tool_starts=0,
            )
        )
    )
    leader_pid = await wait_for_pid_file(marker.with_suffix(".leader-pid"))
    assert await wait_for_process_exit(leader_pid)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.4)
    assert not marker.exists()
    descendant_pid = int(marker.with_suffix(".descendant-pid").read_text(encoding="utf-8"))
    assert await wait_for_process_exit(descendant_pid)


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
    await asyncio.sleep(0)
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


@pytest.mark.parametrize(
    ("allowed_tool_names", "max_tool_starts", "max_records", "max_evidence"),
    [
        ((), 1, 8, 4),
        (("codex_imagegen",), 0, 8, 4),
        (("codex_imagegen", "codex_imagegen"), 1, 8, 4),
        ((), 0, 0, 1),
        ((), 0, 8, 0),
        ((), 0, 8, 9),
    ],
)
def test_request_tool_and_record_bounds_are_validated(
    fake_child: Path,
    allowed_tool_names: tuple[str, ...],
    max_tool_starts: int,
    max_records: int,
    max_evidence: int,
) -> None:
    with pytest.raises(ValueError):
        request_for(
            fake_child,
            "success",
            allowed_tool_names=allowed_tool_names,
            max_tool_starts=max_tool_starts,
            max_jsonl_records=max_records,
            max_evidence_records=max_evidence,
        )
