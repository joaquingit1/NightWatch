"""McpClient that survives LLM failures.

Stock McpClient's thread loop has no error handling: one exception during a
turn (Gemini 429 quota, httpcore.ReadTimeout on a slow kimi response, any
transient network blip) kills the McpClient-thread permanently and the agent
goes silent until a full stack restart. This happened three times today.

This subclass catches per-turn exceptions, repairs the conversation history
(a turn that dies mid-stream can leave a dangling assistant tool_call, which
strict providers reject on the next request), tells the user via the agent
stream, and keeps the loop alive.

Named McpClient so blueprint dedupe replaces the stock module.
"""

from queue import Empty
import time
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from dimos.agents.mcp.mcp_client import McpClient as _StockMcpClient
from dimos.core.core import rpc
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class McpClient(_StockMcpClient):
    @rpc
    def dispatch_continuation(
        self, continuation: dict[str, Any], continuation_context: dict[str, Any]
    ) -> bool:
        """Run autonomous continuations without manufacturing an LLM turn.

        Stock continuation dispatch always enqueues a ``HumanMessage`` after
        the tool call.  That is right for lookout chains, but wrong for the
        curious-mode follow/stop cycle: it marks the agent busy, resets the
        idle clock, and can spend tens of seconds on an unnecessary model call.
        ``_silent`` is private context metadata and never reaches the tool.
        """
        if not continuation_context.get("_silent"):
            super().dispatch_continuation(continuation, continuation_context)
            return True

        tool_name = continuation.get("tool")
        if not tool_name:
            logger.error("Silent continuation missing tool name")
            return False

        tool_args: dict[str, Any] = dict(continuation.get("args", {}))
        for key, value in tool_args.items():
            if isinstance(value, str) and value.startswith("$"):
                context_key = value[1:]
                if context_key in continuation_context:
                    tool_args[key] = continuation_context[context_key]

        try:
            result = self._mcp_tool_call(tool_name, tool_args)
            content = result.get("content", []) if isinstance(result, dict) else []
            response = "\n".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            refused = response.startswith(
                ("Tool not found:", "Cannot start '", "Error running tool '")
            )
            if refused:
                logger.info(
                    "Silent continuation refused",
                    tool=tool_name,
                    response=response[:160],
                )
                return False
            logger.info("Silent continuation executed", tool=tool_name)
            return True
        except Exception:
            logger.exception("Silent continuation failed", tool=tool_name)
            return False

    def _thread_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self._message_queue.get(timeout=0.5)
            except Empty:
                continue

            try:
                with self._lock:
                    if not self._state_graph:
                        raise ValueError("No state graph initialized")
                    self._process_message(self._state_graph, message)
            except Exception as exc:
                logger.exception("Agent turn failed; recovering and continuing")
                self._repair_history()
                try:
                    notice = AIMessage(
                        content=(
                            "I hit an internal error on that request "
                            f"({type(exc).__name__}) but I'm still running. "
                            "Please try again."
                        )
                    )
                    self._history.append(notice)
                    self.agent.publish(notice)
                    self.agent_idle.publish(True)
                except Exception:
                    logger.exception("Failed to publish recovery notice")
                time.sleep(1.0)

    def _repair_history(self) -> None:
        """Append synthetic tool results for any dangling tool_calls."""
        try:
            if not self._history:
                return
            last = self._history[-1]
            calls = getattr(last, "tool_calls", None) or []
            for call in calls:
                self._history.append(
                    ToolMessage(
                        content="(tool call interrupted by an internal error)",
                        tool_call_id=call.get("id", "unknown"),
                    )
                )
        except Exception:
            logger.exception("History repair failed")
