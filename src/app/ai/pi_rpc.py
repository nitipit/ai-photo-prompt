"""Bounded asynchronous JSONL transport for headless Pi RPC workers.

The transport owns only subprocess protocol mechanics.  Adapters decide which
structured tool completions are authoritative; assistant text is collected as
separate context and is never promoted to tool output.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


class PiRPCError(RuntimeError):
    """Base failure raised for a bounded Pi RPC attempt."""

    code: str
    _stdout_tail: bytes
    _stderr_tail: bytes
    _returncode: int | None

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self._stdout_tail = b""
        self._stderr_tail = b""
        self._returncode = None


class PiRPCProtocolError(PiRPCError):
    """The child violated the JSONL RPC contract or confirmation policy."""

    def __init__(self, message: str, *, code: str = "protocol_error") -> None:
        super().__init__(code, message)


class PiRPCOutputLimitError(PiRPCProtocolError):
    """The child exceeded a caller-supplied stdout or stderr bound."""

    def __init__(self, stream: str) -> None:
        self.stream = stream
        super().__init__(f"{stream} output exceeded the configured bound", code="output_limit")


class PiRPCProcessError(PiRPCError):
    """The child could not complete the RPC attempt."""

    def __init__(self, message: str, *, code: str = "process_error") -> None:
        super().__init__(code, message)


class PiRPCTimeoutError(PiRPCError):
    """The hard caller-supplied deadline expired."""

    def __init__(self) -> None:
        super().__init__("timeout", "Pi RPC attempt exceeded its hard timeout")


@dataclass(frozen=True, slots=True)
class PiImageAttachment:
    """A base64-encoded image attachment for the Pi prompt command."""

    data: str
    mime_type: str

    def __post_init__(self) -> None:
        if type(self.data) is not str or not self.data:
            raise ValueError("image attachment data must be a non-empty base64 string")
        if type(self.mime_type) is not str or not self.mime_type:
            raise ValueError("image attachment mime_type must be a non-empty string")

    def as_rpc(self) -> dict[str, str]:
        """Return the Pi RPC ``ImageContent`` mapping."""

        return {"type": "image", "data": self.data, "mimeType": self.mime_type}


@dataclass(frozen=True, slots=True)
class PiRPCRequest:
    """All bounds and authorization for one isolated Pi RPC attempt."""

    argv: Sequence[str]
    cwd: Path
    prompt: str
    timeout: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    attachments: Sequence[PiImageAttachment] = ()
    authorize_confirmation: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "cwd", Path(self.cwd))
        object.__setattr__(self, "attachments", tuple(self.attachments))
        _validate_request(self)


@dataclass(frozen=True, slots=True)
class PiRPCResult:
    """Structured records collected from one settled Pi RPC attempt."""

    command_id: str
    prompt_response: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    tool_completions: tuple[dict[str, Any], ...]
    assistant_text: str
    confirmation_sent: bool


@dataclass(slots=True)
class _ByteCapture:
    limit: int
    total: int = 0
    _tail: deque[bytes] | None = None
    _tail_size: int = 0

    def __post_init__(self) -> None:
        self._tail = deque()

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if not chunk or self.limit == 0:
            return
        assert self._tail is not None
        if len(chunk) >= self.limit:
            self._tail.clear()
            self._tail.append(chunk[-self.limit :])
            self._tail_size = self.limit
            return
        self._tail.append(chunk)
        self._tail_size += len(chunk)
        while self._tail_size > self.limit:
            removed = self._tail.popleft()
            self._tail_size -= len(removed)

    @property
    def tail(self) -> bytes:
        assert self._tail is not None
        return b"".join(self._tail)


@dataclass(frozen=True, slots=True)
class _Record:
    value: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _StreamEnd:
    stream: str


@dataclass(frozen=True, slots=True)
class _ReaderFailure:
    stream: str
    reason: str


_QueueItem = _Record | _StreamEnd | _ReaderFailure


def _validate_request(request: PiRPCRequest) -> None:
    if not request.argv or any(
        type(argument) is not str or not argument for argument in request.argv
    ):
        raise ValueError("argv must contain a non-empty executable and string arguments")
    if type(request.prompt) is not str or not request.prompt:
        raise ValueError("prompt must be a non-empty string")
    if not isinstance(request.timeout, (int, float)) or isinstance(request.timeout, bool):
        raise TypeError("timeout must be a number")
    if not math.isfinite(request.timeout) or request.timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    for name, value in (
        ("max_stdout_bytes", request.max_stdout_bytes),
        ("max_stderr_bytes", request.max_stderr_bytes),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if type(request.authorize_confirmation) is not bool:
        raise TypeError("authorize_confirmation must be a boolean")
    if any(not isinstance(item, PiImageAttachment) for item in request.attachments):
        raise TypeError("attachments must contain PiImageAttachment values")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_record(line: bytes) -> dict[str, Any] | str:
    if line.endswith(b"\r"):
        line = line[:-1]
    if not line:
        return "empty JSONL record"
    try:
        text = line.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return "malformed UTF-8 or JSON record"
    if not isinstance(value, dict):
        return "JSONL record must be an object"
    return cast(dict[str, Any], value)


async def _read_stdout(
    stream: asyncio.StreamReader,
    queue: asyncio.Queue[_QueueItem],
    capture: _ByteCapture,
) -> None:
    buffer = bytearray()
    failed = False
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            capture.add(chunk)
            if capture.total > capture.limit:
                if not failed:
                    await queue.put(_ReaderFailure("stdout", "output_limit"))
                failed = True
                continue
            if failed:
                continue
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                parsed = _parse_record(line)
                if isinstance(parsed, str):
                    await queue.put(_ReaderFailure("stdout", parsed))
                    failed = True
                    buffer.clear()
                    break
                await queue.put(_Record(parsed))
        if not failed and buffer:
            parsed = _parse_record(bytes(buffer))
            if isinstance(parsed, str):
                await queue.put(_ReaderFailure("stdout", parsed))
            else:
                await queue.put(_Record(parsed))
    except asyncio.CancelledError:
        raise
    except Exception:
        if not failed:
            await queue.put(_ReaderFailure("stdout", "stream_read_error"))
    finally:
        await queue.put(_StreamEnd("stdout"))


async def _read_stderr(
    stream: asyncio.StreamReader,
    queue: asyncio.Queue[_QueueItem],
    capture: _ByteCapture,
) -> None:
    failed = False
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            capture.add(chunk)
            if capture.total > capture.limit and not failed:
                await queue.put(_ReaderFailure("stderr", "output_limit"))
                failed = True
    except asyncio.CancelledError:
        raise
    except Exception:
        if not failed:
            await queue.put(_ReaderFailure("stderr", "stream_read_error"))
    finally:
        await queue.put(_StreamEnd("stderr"))


async def _close_stdin(process: asyncio.subprocess.Process) -> None:
    if process.stdin is None:
        return
    process.stdin.close()
    try:
        await asyncio.wait_for(process.stdin.wait_closed(), timeout=0.2)
    except (TimeoutError, BrokenPipeError, ConnectionError, OSError):
        pass


def _signal_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig is signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _cleanup_process(
    process: asyncio.subprocess.Process,
    reader_tasks: Sequence[asyncio.Task[None]],
    process_wait: asyncio.Task[int],
) -> None:
    await _close_stdin(process)
    _signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except TimeoutError:
        _signal_group(process, signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            pass
    for task in reader_tasks:
        if task.done():
            continue
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except TimeoutError:
            task.cancel()
    try:
        await asyncio.gather(*reader_tasks, return_exceptions=True)
    finally:
        if not process_wait.done():
            process_wait.cancel()
        await asyncio.gather(process_wait, return_exceptions=True)


async def _cleanup_resilient(
    process: asyncio.subprocess.Process,
    reader_tasks: Sequence[asyncio.Task[None]],
    process_wait: asyncio.Task[int],
) -> None:
    cleanup = asyncio.create_task(_cleanup_process(process, reader_tasks, process_wait))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    await asyncio.shield(cleanup)
    if cancelled:
        raise asyncio.CancelledError


async def _write_json(
    process: asyncio.subprocess.Process,
    value: Mapping[str, Any],
    deadline: float,
) -> None:
    if process.stdin is None:
        raise PiRPCProcessError("child stdin is unavailable", code="stdin_unavailable")
    try:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        process.stdin.write(payload)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise PiRPCTimeoutError
        await asyncio.wait_for(process.stdin.drain(), timeout=remaining)
    except TimeoutError as error:
        raise PiRPCTimeoutError from error
    except (BrokenPipeError, ConnectionError, OSError) as error:
        raise PiRPCProcessError(
            "child stdin closed before the command was sent", code="stdin_closed"
        ) from error


def _attach_failure(
    error: PiRPCError,
    stdout_capture: _ByteCapture,
    stderr_capture: _ByteCapture,
    process: asyncio.subprocess.Process | None,
) -> PiRPCError:
    error._stdout_tail = stdout_capture.tail
    error._stderr_tail = stderr_capture.tail
    error._returncode = process.returncode if process is not None else None
    return error


async def run_pi_rpc(request: PiRPCRequest) -> PiRPCResult:
    """Run one bounded Pi RPC request and return structured protocol evidence.

    ``authorize_confirmation`` allows one correlated ``confirm`` UI request to
    receive a positive response.  A false value receives a negative response
    and fails the attempt, so adapters cannot accidentally consume quota.
    """

    _validate_request(request)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + request.timeout
    command_id = f"pi-prompt-{uuid.uuid4().hex}"
    prompt: dict[str, Any] = {
        "id": command_id,
        "type": "prompt",
        "message": request.prompt,
    }
    if request.attachments:
        prompt["images"] = [attachment.as_rpc() for attachment in request.attachments]

    process: asyncio.subprocess.Process | None = None
    reader_tasks: list[asyncio.Task[None]] = []
    process_wait: asyncio.Task[int] | None = None
    stdout_capture = _ByteCapture(request.max_stdout_bytes)
    stderr_capture = _ByteCapture(request.max_stderr_bytes)

    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=request.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            raise PiRPCProcessError("could not start Pi RPC child", code="spawn_error") from error

        assert process.stdout is not None and process.stderr is not None
        queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        reader_tasks = [
            asyncio.create_task(_read_stdout(process.stdout, queue, stdout_capture)),
            asyncio.create_task(_read_stderr(process.stderr, queue, stderr_capture)),
        ]
        process_wait = asyncio.create_task(process.wait())
        await _write_json(process, prompt, deadline)

        events: list[dict[str, Any]] = []
        tool_completions: list[dict[str, Any]] = []
        completed_tool_call_ids: set[str] = set()
        started_tool_names: dict[str, str] = {}
        prompt_response: dict[str, Any] | None = None
        assistant_text = ""
        confirmation_id: str | None = None
        confirmation_sent = False
        settled = False
        stdout_ended = False

        async def handle_item(item: _QueueItem) -> None:
            nonlocal assistant_text, confirmation_id, confirmation_sent, prompt_response
            nonlocal settled, stdout_ended
            if isinstance(item, _ReaderFailure):
                if item.reason == "output_limit":
                    raise PiRPCOutputLimitError(item.stream)
                if item.stream == "stdout":
                    raise PiRPCProtocolError("stdout contained malformed or unreadable JSONL")
                raise PiRPCProcessError(
                    "child stderr could not be drained", code="stderr_read_error"
                )
            if isinstance(item, _StreamEnd):
                if item.stream == "stdout":
                    stdout_ended = True
                    if not settled:
                        exit_code = process.returncode
                        if process_wait is not None and process_wait.done():
                            exit_code = process_wait.result()
                        if exit_code not in (None, 0):
                            raise PiRPCProcessError(
                                "Pi RPC child exited with an error", code="child_error"
                            )
                        raise PiRPCProcessError(
                            "child stdout ended before agent_settled", code="eof_before_settle"
                        )
                return

            event = item.value
            if settled:
                raise PiRPCProtocolError("event arrived after agent_settled", code="late_event")
            events.append(event)
            event_type = event.get("type")

            if (
                event_type
                in {
                    "tool_execution_start",
                    "tool_execution_update",
                    "tool_execution_end",
                }
                and prompt_response is None
            ):
                raise PiRPCProtocolError(
                    "tool event arrived before the prompt response",
                    code="tool_before_prompt_response",
                )

            if event_type == "response":
                if prompt_response is not None:
                    raise PiRPCProtocolError("duplicate prompt response", code="duplicate_response")
                if event.get("id") != command_id:
                    raise PiRPCProtocolError("prompt response ID did not match", code="id_mismatch")
                if event.get("command") != "prompt":
                    raise PiRPCProtocolError(
                        "response was not for prompt", code="response_mismatch"
                    )
                if event.get("success") is not True:
                    raise PiRPCProtocolError(
                        "prompt was rejected by the child", code="prompt_rejected"
                    )
                prompt_response = event
                return

            if event_type == "extension_ui_request":
                if prompt_response is None:
                    raise PiRPCProtocolError(
                        "confirmation was not correlated to the accepted prompt",
                        code="confirmation_mismatch",
                    )
                request_id = event.get("id")
                if type(request_id) is not str or not request_id:
                    raise PiRPCProtocolError(
                        "confirmation request had no valid ID", code="confirmation_mismatch"
                    )
                if event.get("method") != "confirm":
                    raise PiRPCProtocolError(
                        "unexpected extension UI request", code="unexpected_ui_request"
                    )
                if confirmation_id is not None:
                    raise PiRPCProtocolError(
                        "duplicate confirmation request", code="confirmation_duplicate"
                    )
                confirmation_id = request_id
                confirmation_sent = True
                await _write_json(
                    process,
                    {
                        "type": "extension_ui_response",
                        "id": request_id,
                        "confirmed": request.authorize_confirmation,
                    },
                    deadline,
                )
                if not request.authorize_confirmation:
                    raise PiRPCProtocolError(
                        "confirmation denied by caller authorization",
                        code="confirmation_denied",
                    )
                return

            if event_type in {
                "tool_execution_start",
                "tool_execution_update",
                "tool_execution_end",
            }:
                tool_call_id = event.get("toolCallId")
                tool_name = event.get("toolName")
                if type(tool_call_id) is not str or not tool_call_id:
                    raise PiRPCProtocolError(
                        "tool event had no valid call ID", code="tool_id_mismatch"
                    )
                if type(tool_name) is not str or not tool_name:
                    raise PiRPCProtocolError(
                        "tool event had no valid tool name", code="tool_event_invalid"
                    )

                if event_type == "tool_execution_start":
                    if tool_call_id in started_tool_names:
                        raise PiRPCProtocolError(
                            "tool start was duplicated", code="duplicate_tool_start"
                        )
                    started_tool_names[tool_call_id] = tool_name
                    return

                started_name = started_tool_names.get(tool_call_id)
                if started_name is None:
                    raise PiRPCProtocolError(
                        "tool event had no matching start", code="tool_start_missing"
                    )
                if started_name != tool_name:
                    raise PiRPCProtocolError(
                        "tool event name did not match its start",
                        code="tool_name_mismatch",
                    )

                if event_type == "tool_execution_update":
                    if tool_call_id in completed_tool_call_ids:
                        raise PiRPCProtocolError(
                            "tool update arrived after completion", code="tool_after_completion"
                        )
                    return

                if type(event.get("isError")) is not bool or not isinstance(
                    event.get("result"), dict
                ):
                    raise PiRPCProtocolError(
                        "tool completion was not structured", code="tool_event_invalid"
                    )
                if tool_call_id in completed_tool_call_ids:
                    raise PiRPCProtocolError(
                        "tool completion was duplicated", code="duplicate_tool_completion"
                    )
                completed_tool_call_ids.add(tool_call_id)
                tool_completions.append(event)
                return

            if event_type == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, str):
                        assistant_text = content
                    elif isinstance(content, list):
                        assistant_text = "".join(
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict)
                            and block.get("type") == "text"
                            and isinstance(block.get("text"), str)
                        )
                return

            if event_type == "agent_settled":
                if prompt_response is None:
                    raise PiRPCProtocolError(
                        "agent_settled arrived before the prompt response",
                        code="prompt_response_missing",
                    )
                settled = True
                return

        while not settled:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PiRPCTimeoutError
            get_item = asyncio.create_task(queue.get())
            assert process_wait is not None
            done, _ = await asyncio.wait(
                {get_item, process_wait},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                get_item.cancel()
                await asyncio.gather(get_item, return_exceptions=True)
                raise PiRPCTimeoutError
            if get_item in done:
                await handle_item(get_item.result())
                continue
            get_item.cancel()
            await asyncio.gather(get_item, return_exceptions=True)
            await asyncio.sleep(0)
            if not queue.empty():
                await handle_item(queue.get_nowait())
                continue
            if process.returncode not in (None, 0):
                raise PiRPCProcessError("Pi RPC child exited with an error", code="child_error")
            raise PiRPCProcessError(
                "Pi RPC child exited before agent_settled", code="eof_before_settle"
            )

        # Give already-buffered records a bounded chance to expose a late event.
        settle_deadline = min(deadline, loop.time() + 0.05)
        while loop.time() < settle_deadline:
            remaining = settle_deadline - loop.time()
            try:
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                break
            await handle_item(item)

        if (
            not prompt_response
            or not settled
            or not stdout_ended
            and process.returncode not in (None, 0)
        ):
            raise PiRPCProcessError("Pi RPC child did not settle cleanly", code="child_error")
        return PiRPCResult(
            command_id=command_id,
            prompt_response=prompt_response,
            events=tuple(events),
            tool_completions=tuple(tool_completions),
            assistant_text=assistant_text,
            confirmation_sent=confirmation_sent,
        )
    except asyncio.CancelledError:
        raise
    except PiRPCError as error:
        raise _attach_failure(error, stdout_capture, stderr_capture, process) from None
    finally:
        if process is not None and process_wait is not None:
            await _cleanup_resilient(process, reader_tasks, process_wait)


async def run_rpc(
    *,
    argv: Sequence[str],
    cwd: Path,
    prompt: str,
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    attachments: Sequence[PiImageAttachment] = (),
    authorize_confirmation: bool = False,
) -> PiRPCResult:
    """Convenience wrapper for callers that do not need a request object."""

    return await run_pi_rpc(
        PiRPCRequest(
            argv=argv,
            cwd=cwd,
            prompt=prompt,
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            attachments=attachments,
            authorize_confirmation=authorize_confirmation,
        )
    )


__all__ = [
    "PiImageAttachment",
    "PiRPCError",
    "PiRPCOutputLimitError",
    "PiRPCProcessError",
    "PiRPCProtocolError",
    "PiRPCRequest",
    "PiRPCResult",
    "PiRPCTimeoutError",
    "run_pi_rpc",
    "run_rpc",
]
