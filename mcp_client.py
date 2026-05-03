import asyncio
import logging
import queue
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCNotification, JSONRPCRequest, JSONRPCResponse, JSONRPCError,
    CancelledNotification, ProgressNotification, ErrorData,
)
from builtin_tools import BUILTIN_TOOLS, execute_builtin_tool, set_work_dir

logger = logging.getLogger(__name__)

# The wechat server uses this custom notification method
WECHAT_CHANNEL_METHOD = "notifications/claude/channel"
_HIDDEN_MODEL_TOOL_PREFIXES = ("wechat_",)


def _is_hidden_model_tool(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _HIDDEN_MODEL_TOOL_PREFIXES)


class NotifyingClientSession(ClientSession):
    """ClientSession that captures incoming notifications into a queue.

    Overrides _receive_loop to intercept custom notifications (like
    notifications/claude/channel) before Pydantic validation rejects them.
    """

    def __init__(self, *args, notification_queue=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._notification_queue = notification_queue

    async def _receive_loop(self) -> None:
        async with (
            self._read_stream,
            self._write_stream,
        ):
            try:
                async for message in self._read_stream:
                    if isinstance(message, Exception):
                        await self._handle_incoming(message)
                    elif isinstance(message.message.root, JSONRPCRequest):
                        try:
                            validated_request = self._receive_request_type.model_validate(
                                message.message.root.model_dump(
                                    by_alias=True, mode="json", exclude_none=True
                                )
                            )
                            from mcp.shared.session import RequestResponder
                            responder = RequestResponder(
                                request_id=message.message.root.id,
                                request_meta=(
                                    validated_request.root.params.meta
                                    if validated_request.root.params
                                    else None
                                ),
                                request=validated_request,
                                session=self,
                                on_complete=lambda r: self._in_flight.pop(
                                    r.request_id, None
                                ),
                                message_metadata=message.metadata,
                            )
                            self._in_flight[responder.request_id] = responder
                            await self._received_request(responder)
                            if not responder._completed:
                                await self._handle_incoming(responder)
                        except Exception as e:
                            logging.warning(f"Failed to validate request: {e}")
                            error_response = JSONRPCError(
                                jsonrpc="2.0",
                                id=message.message.root.id,
                                error=ErrorData(
                                    code=-32602,
                                    message="Invalid request parameters",
                                    data="",
                                ),
                            )
                            await self._write_stream.send(
                                SessionMessage(
                                    message=JSONRPCNotification(
                                        error_response
                                    ) if False else None
                                )
                            )

                    elif isinstance(message.message.root, JSONRPCNotification):
                        raw = message.message.root
                        # Intercept custom notifications before validation
                        if raw.method == WECHAT_CHANNEL_METHOD:
                            if self._notification_queue is not None:
                                self._notification_queue.put_nowait(raw)
                            continue
                        try:
                            notification = (
                                self._receive_notification_type.model_validate(
                                    raw.model_dump(
                                        by_alias=True,
                                        mode="json",
                                        exclude_none=True,
                                    )
                                )
                            )
                            if isinstance(notification.root, CancelledNotification):
                                cancelled_id = notification.root.params.requestId
                                if cancelled_id in self._in_flight:
                                    await self._in_flight[cancelled_id].cancel()
                            else:
                                if isinstance(notification.root, ProgressNotification):
                                    progress_token = (
                                        notification.root.params.progressToken
                                    )
                                    if progress_token in self._progress_callbacks:
                                        callback = self._progress_callbacks[
                                            progress_token
                                        ]
                                        try:
                                            await callback(
                                                notification.root.params.progress,
                                                notification.root.params.total,
                                                notification.root.params.message,
                                            )
                                        except Exception as e:
                                            logging.error(
                                                "Progress callback raised: %s", e
                                            )
                                await self._received_notification(notification)
                                await self._handle_incoming(notification)
                        except Exception as e:
                            logging.warning(
                                f"Failed to validate notification: {e}. "
                                f"Message: {raw}"
                            )
                    else:
                        await self._handle_response(message)

            except Exception as e:
                logging.exception(f"Unhandled exception in receive loop: {e}")
            finally:
                for id, stream in list(self._response_streams.items()):
                    error = ErrorData(code=-32000, message="Connection closed")
                    try:
                        await stream.send(
                            JSONRPCError(jsonrpc="2.0", id=id, error=error)
                        )
                        await stream.aclose()
                    except Exception:
                        pass
                self._response_streams.clear()


class MCPManager:
    def __init__(self):
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, dict] = {}
        self._tool_to_server: dict[str, str] = {}
        self._builtin_tools: dict[str, dict] = {}
        self._cm: AsyncExitStack | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._notification_queue: queue.Queue = queue.Queue()
        self._register_builtin_tools()

    def _register_builtin_tools(self):
        """Register built-in tools (Read, Write, Edit, Bash)."""
        for tool_def in BUILTIN_TOOLS:
            self._builtin_tools[tool_def["name"]] = {
                "name": tool_def["name"],
                "description": tool_def["description"],
                "input_schema": tool_def["input_schema"],
            }

    def set_work_dir(self, path: str):
        set_work_dir(path)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, daemon=True
            )
            self._thread.start()
        return self._loop

    def _run_async(self, coro):
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=60)

    async def connect_all(self, configs: list) -> None:
        self._cm = AsyncExitStack()
        await self._cm.__aenter__()

        for config in configs:
            try:
                server_params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env=config.env,
                )
                transport = await self._cm.enter_async_context(
                    stdio_client(server_params)
                )
                read, write = transport
                session = await self._cm.enter_async_context(
                    NotifyingClientSession(
                        read, write,
                        notification_queue=self._notification_queue,
                    )
                )
                await session.initialize()

                tools_response = await session.list_tools()
                self._sessions[config.name] = session

                for tool in tools_response.tools:
                    tool_name = tool.name
                    if tool_name in self._tools:
                        tool_name = f"{config.name}_{tool_name}"
                        logger.warning(
                            f"Tool name collision: {tool.name} renamed to {tool_name}"
                        )
                    self._tools[tool_name] = {
                        "name": tool_name,
                        "description": tool.description or f"Tool: {tool.name}",
                        "input_schema": tool.inputSchema,
                        "_original_name": tool.name,
                    }
                    self._tool_to_server[tool_name] = config.name

                logger.info(
                    f"Connected to MCP server '{config.name}' with tools: "
                    f"{[t.name for t in tools_response.tools]}"
                )
            except Exception as e:
                logger.warning(f"Failed to connect to MCP server '{config.name}': {e}")

    def connect_all_sync(self, configs: list) -> None:
        self._run_async(self.connect_all(configs))

    async def disconnect_all(self) -> None:
        if self._cm:
            await self._cm.aclose()
            self._cm = None
            self._sessions.clear()
            self._tools.clear()
            self._tool_to_server.clear()

    def disconnect_all_sync(self) -> None:
        if self._loop and not self._loop.is_closed():
            self._run_async(self.disconnect_all())
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._loop = None
            self._thread = None

    def get_claude_tools(self) -> list:
        tools = []
        for tool_info in self._tools.values():
            if _is_hidden_model_tool(tool_info["name"]):
                continue
            tools.append({
                "name": tool_info["name"],
                "description": tool_info["description"],
                "input_schema": tool_info["input_schema"],
            })
        # Include built-in tools
        for tool_info in self._builtin_tools.values():
            tools.append({
                "name": tool_info["name"],
                "description": tool_info["description"],
                "input_schema": tool_info["input_schema"],
            })
        return tools

    def get_claude_tools_filtered(self, allowed_tools: list[str] | None = None) -> list:
        """Get tools, optionally filtering built-in tools by allowed_tools list."""
        tools = []
        for tool_info in self._tools.values():
            if _is_hidden_model_tool(tool_info["name"]):
                continue
            tools.append({
                "name": tool_info["name"],
                "description": tool_info["description"],
                "input_schema": tool_info["input_schema"],
            })
        # Include built-in tools (filtered if allowed_tools specified)
        for name, tool_info in self._builtin_tools.items():
            if allowed_tools is None or name in allowed_tools:
                tools.append({
                    "name": tool_info["name"],
                    "description": tool_info["description"],
                    "input_schema": tool_info["input_schema"],
                })
        return tools

    def get_notification(self, block=False, timeout=0.5):
        try:
            return self._notification_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def call_tool(self, name: str, arguments: dict) -> str:
        # Handle built-in tools
        if name in self._builtin_tools:
            return execute_builtin_tool(name, arguments)
        try:
            return self._run_async(self._call_tool_async(name, arguments))
        except FutureTimeoutError:
            return f"Error calling tool '{name}': timed out"

    async def _call_tool_async(self, name: str, arguments: dict) -> str:
        if name not in self._tool_to_server:
            return f"Error: Unknown tool '{name}'"

        server_name = self._tool_to_server[name]
        session = self._sessions.get(server_name)
        if not session:
            return f"Error: MCP server '{server_name}' not connected"

        try:
            original_name = self._tools[name].get("_original_name", name)
            result = await asyncio.wait_for(
                session.call_tool(original_name, arguments=arguments),
                timeout=45,
            )

            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                elif hasattr(content, "data"):
                    parts.append(f"[Binary data: {content.mimeType}]")
                else:
                    parts.append(str(content))

            text = "\n".join(parts)
            if result.isError:
                text = f"Error: {text}"
            return text
        except Exception as e:
            return f"Error calling tool '{name}': {e}"

    def get_available_tools_summary(self) -> str:
        all_tools = list(self._tools.keys()) + list(self._builtin_tools.keys())
        if not all_tools:
            return "No tools available"
        return f"{len(all_tools)} tools: {', '.join(all_tools)}"
