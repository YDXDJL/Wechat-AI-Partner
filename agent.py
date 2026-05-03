import logging
import re
from typing import Any

import anthropic
import openai

from config import AgentConfig
from models import ModelConfig
from history import save_history, load_history, clear_history
from mcp_client import MCPManager

logger = logging.getLogger(__name__)
_HIDDEN_WECHAT_TOOLS = {"wechat_reply", "wechat_typing", "wechat_send_file"}
_TOOL_USE_INSTRUCTIONS = (
    "\n\n## Reality-check and tool use\n"
    "Before answering, silently decide whether your own knowledge and the conversation context are enough. "
    "If they are enough, answer directly. If the recent conversation already contains a close matching topic, "
    "use that context first. If the answer depends on current time, today's weather, recent news, live facts, "
    "local places, restaurants, prices, or other real-world information not already in context, use the available "
    "tools such as CurrentTime, Weather, WebSearch, and WebFetch before answering. "
    "For restaurant/place recommendations, infer the location from context when reasonable; if the user has been "
    "talking about Guangzhou, search around Guangzhou instead of asking again. "
    "After using tools, answer naturally in your current persona or skill identity. "
    "Do not mention tool mechanics unless the user asks."
)

# Meta-commentary patterns to strip when skill is active
_META_PATTERNS = [
    r"^嗯[,，].*已经按照",
    r"^好的[,，].*已经按照",
    r"^已.*按照.*人设",
    r"^已经按照.*回复了",
    r"^用.*的方式来表达",
    r"^保持了.*角色.*特点",
    r"^现在等待用户",
    r"^我.*已经.*按照",
    r"^根据.*设定.*回复",
    r"^我来.*按照",
]
_META_RE = re.compile("|".join(_META_PATTERNS), re.IGNORECASE)


def _strip_meta_commentary(text: str) -> str:
    """Remove meta-commentary from LLM output, keeping only character dialogue."""
    lines = text.strip().split("\n")
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _META_RE.search(stripped):
            continue
        clean_lines.append(line)
    result = "\n".join(clean_lines).strip()
    return result if result else text.strip()


def _mcp_tools_to_openai(tools: list) -> list:
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        })
    return result


def _mcp_tools_to_claude(tools: list) -> list:
    return tools


def _openai_response_to_messages(response) -> tuple:
    """Extract text and tool_calls from an OpenAI response."""
    text_parts = []
    tool_calls = []
    for choice in response.choices:
        msg = choice.message
        if msg.content:
            text_parts.append(msg.content)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                import json
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })
    return "\n".join(text_parts), tool_calls


class Agent:
    def __init__(
        self,
        agent_config: AgentConfig,
        model_config: ModelConfig,
        mcp_manager: MCPManager,
        base_dir: str = ".",
        extra_instructions: str = "",
        account_id: str = None,
    ):
        self.agent_config = agent_config
        self.model_config = model_config
        self.mcp = mcp_manager
        self.base_dir = base_dir
        self.extra_instructions = extra_instructions
        self.account_id = account_id
        self._allowed_tools: list[str] | None = None
        self._persistent_skill: str | None = None  # skill name bound to this agent
        self._persistent_prompt: str | None = None  # persistent skill prompt
        self.messages: list = self._normalize_legacy_history(load_history(base_dir, account_id))
        self._init_clients()

    def _init_clients(self):
        mc = self.model_config
        if mc.provider == "claude":
            self._claude_client = anthropic.Anthropic(
                api_key=mc.api_key,
                base_url=mc.base_url if mc.base_url != "https://api.anthropic.com" else None,
            )
            self._openai_client = None
        else:
            self._openai_client = openai.OpenAI(
                api_key=mc.api_key,
                base_url=mc.base_url,
            )
            self._claude_client = None

    def switch_model(self, model_config: ModelConfig) -> None:
        self.model_config = model_config
        self._init_clients()

    def switch_account(self, account_id: str) -> None:
        """Switch to a different WeChat account's history."""
        self.account_id = account_id
        self.messages = self._normalize_legacy_history(load_history(self.base_dir, account_id))

    def _normalize_legacy_history(self, messages: list) -> list:
        """Remove old model-visible WeChat send tool calls from persisted history."""
        normalized = []
        hidden_tool_ids: set[str] = set()
        previous_from_hidden_wechat_reply = False
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = []
                converted_hidden_wechat_reply = False
                has_tool_use = False
                for block in content:
                    if not isinstance(block, dict):
                        new_content.append(block)
                        continue
                    if block.get("type") == "thinking":
                        continue
                    if block.get("type") == "tool_use" and block.get("name") in _HIDDEN_WECHAT_TOOLS:
                        tool_id = block.get("id")
                        if tool_id:
                            hidden_tool_ids.add(tool_id)
                        if block.get("name") == "wechat_reply":
                            text = (block.get("input") or {}).get("text")
                            if text:
                                new_content.append({"type": "text", "text": text})
                                converted_hidden_wechat_reply = True
                        continue
                    if block.get("type") == "tool_result" and block.get("tool_use_id") in hidden_tool_ids:
                        continue
                    if block.get("type") == "tool_use":
                        has_tool_use = True
                    new_content.append(block)
                if not new_content:
                    continue
                only_text = all(isinstance(b, dict) and b.get("type") == "text" for b in new_content)
                if msg.get("role") == "assistant" and previous_from_hidden_wechat_reply and only_text and not has_tool_use:
                    previous_from_hidden_wechat_reply = False
                    continue
                normalized.append({**msg, "content": new_content})
                previous_from_hidden_wechat_reply = bool(
                    msg.get("role") == "assistant" and converted_hidden_wechat_reply
                )
            else:
                normalized.append(msg)
                previous_from_hidden_wechat_reply = False
        return normalized

    def set_skill_instructions(self, instructions: str) -> None:
        """Set extra instructions from a skill (injected into system prompt)."""
        self.extra_instructions = instructions

    def clear_skill_instructions(self) -> None:
        """Clear one-shot skill instructions, restore persistent skill if any."""
        self.extra_instructions = self._persistent_prompt or ""

    def set_allowed_tools(self, tools: list[str] | None) -> None:
        """Set allowed built-in tools for current skill execution."""
        self._allowed_tools = tools

    def set_persistent_skill(self, skill_prompt: str | None, skill_name: str | None = None):
        """Set a skill prompt that persists across all runs (for account-bound skills)."""
        old_prompt = self._persistent_prompt
        self._persistent_prompt = skill_prompt
        self._persistent_skill = skill_name
        # Update extra_instructions based on priority: one-shot > persistent
        if not self.extra_instructions or self.extra_instructions == old_prompt:
            self.extra_instructions = skill_prompt or ""

    def get_persistent_skill(self) -> str | None:
        return self._persistent_skill

    def run(self, user_input: str | list[dict[str, Any]]) -> str:
        self.messages.append({"role": "user", "content": user_input})

        if self._allowed_tools is not None:
            tools = self.mcp.get_claude_tools_filtered(self._allowed_tools)
        else:
            tools = self.mcp.get_claude_tools()

        if self.model_config.provider == "claude":
            result = self._run_claude(tools)
        else:
            result = self._run_openai(tools)

        # Strip meta-commentary when a persistent skill is active
        if self._persistent_prompt:
            result = _strip_meta_commentary(result)

        self._sanitize_image_blocks_for_history()
        save_history(self.messages, self.base_dir, self.account_id)
        return result

    def _sanitize_image_blocks_for_history(self) -> None:
        """Replace base64 image blocks with text summaries before persistence/reuse."""
        for msg in self.messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            cleaned = []
            for block in content:
                block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                if block_type == "thinking":
                    continue
                if isinstance(block, dict) and block.get("type") == "image":
                    previous_text = cleaned[-1].get("text", "") if cleaned and isinstance(cleaned[-1], dict) else ""
                    if previous_text.startswith("【图片】"):
                        continue
                    source = block.get("source") or {}
                    media_type = source.get("media_type", "image/unknown")
                    data = source.get("data") or ""
                    approx_bytes = int(len(data) * 3 / 4) if isinstance(data, str) else 0
                    cleaned.append({
                        "type": "text",
                        "text": f"【图片】用户发送了一张图片\n格式: {media_type}\n大小约: {approx_bytes} bytes",
                    })
                elif isinstance(block, dict) and block.get("type") == "image_url":
                    image_url = block.get("image_url") or {}
                    url = image_url.get("url", "")
                    media_type = "image/unknown"
                    approx_bytes = 0
                    if isinstance(url, str) and url.startswith("data:"):
                        header, _, data = url.partition(",")
                        media_type = header.removeprefix("data:").split(";", 1)[0] or media_type
                        approx_bytes = int(len(data) * 3 / 4)
                    cleaned.append({
                        "type": "text",
                        "text": f"【图片】用户发送了一张图片\n格式: {media_type}\n大小约: {approx_bytes} bytes",
                    })
                else:
                    cleaned.append(block)
            msg["content"] = cleaned

    def _get_system_prompt(self) -> str:
        # Skill instructions take persona control; operational tool rules still apply.
        if self.extra_instructions:
            return self.extra_instructions + _TOOL_USE_INSTRUCTIONS
        return (self.agent_config.system_prompt or "") + _TOOL_USE_INSTRUCTIONS

    def _run_claude(self, tools: list) -> str:
        claude_tools = _mcp_tools_to_claude(tools)
        system_prompt = self._get_system_prompt()

        for _ in range(self.agent_config.max_tool_rounds):
            kwargs = {
                "model": self.model_config.model,
                "max_tokens": self.agent_config.max_tokens,
                "messages": self.messages,
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            if claude_tools:
                kwargs["tools"] = claude_tools
            if self.agent_config.thinking:
                kwargs["thinking"] = {"type": "adaptive"}

            response = self._claude_client.messages.create(**kwargs)
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break
            elif response.stop_reason == "pause_turn":
                continue
            elif response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name in _HIDDEN_WECHAT_TOOLS:
                        logger.warning(f"Ignoring hidden WeChat tool requested by model: {block.name}")
                        continue
                    result_text = self.mcp.call_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
            if tool_results:
                self.messages.append({"role": "user", "content": tool_results})

        return self._extract_text_claude()

    def _run_openai(self, tools: list) -> str:
        openai_tools = _mcp_tools_to_openai(tools)
        system_prompt = self._get_system_prompt()

        openai_messages = []
        if system_prompt:
            openai_messages.append({
                "role": "system",
                "content": system_prompt,
            })
        for msg in self.messages:
            if msg["role"] == "assistant":
                content = msg.get("content", "")
                if isinstance(content, list):
                    text_parts = []
                    tool_calls_raw = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                import json
                                tool_calls_raw.append({
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name", ""),
                                        "arguments": json.dumps(block.get("input", {})),
                                    },
                                })
                    openai_messages.append({
                        "role": "assistant",
                        "content": "\n".join(text_parts) if text_parts else None,
                        "tool_calls": tool_calls_raw if tool_calls_raw else None,
                    })
                else:
                    openai_messages.append({"role": "assistant", "content": content})
            elif msg["role"] == "user":
                content = msg.get("content", "")
                if isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                    for tr in content:
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id", ""),
                            "content": tr.get("content", ""),
                        })
                elif isinstance(content, list):
                    converted = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            converted.append({"type": "text", "text": block.get("text", "")})
                        elif block.get("type") == "image":
                            source = block.get("source") or {}
                            media_type = source.get("media_type", "image/jpeg")
                            data = source.get("data", "")
                            converted.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{data}"},
                            })
                    openai_messages.append({"role": "user", "content": converted})
                else:
                    openai_messages.append({"role": "user", "content": content})
            else:
                openai_messages.append(msg)

        for _ in range(self.agent_config.max_tool_rounds):
            kwargs = {
                "model": self.model_config.model,
                "messages": openai_messages,
                "max_tokens": self.agent_config.max_tokens,
            }
            if openai_tools:
                kwargs["tools"] = openai_tools

            response = self._openai_client.chat.completions.create(**kwargs)

            choice = response.choices[0]
            msg = choice.message

            assistant_entry = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            openai_messages.append(assistant_entry)

            if choice.finish_reason == "stop" or not msg.tool_calls:
                break

            if msg.tool_calls:
                import json
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    result_text = self.mcp.call_tool(tc.function.name, args)
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    })

        self.messages = self._openai_to_internal(openai_messages)
        return self._extract_text_openai(openai_messages)

    def _openai_to_internal(self, openai_messages: list) -> list:
        result = []
        for msg in openai_messages:
            if msg["role"] == "system":
                continue
            entry = {"role": msg["role"], "content": msg.get("content", "")}
            result.append(entry)
        return result

    def _extract_text_claude(self) -> str:
        if not self.messages:
            return ""
        last = self.messages[-1]
        content = last.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif hasattr(block, "type") and block.type == "text":
                    parts.append(block.text)
            return "\n".join(parts)
        return str(content)

    def _extract_text_openai(self, openai_messages: list) -> str:
        for msg in reversed(openai_messages):
            if msg["role"] == "assistant":
                return msg.get("content", "")
        return ""

    def run_scheduled(self, prompt: str) -> str:
        self.messages = []
        return self.run(prompt)

    def reset(self) -> None:
        self.messages = []
        clear_history(self.base_dir, self.account_id)
