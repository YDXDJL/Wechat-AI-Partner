import logging
import os
import queue
import random
import re
import shutil
import threading
import time
import json
from html import escape

from image_utils import ImageAttachment, build_multimodal_content, load_image_attachment
from config import load_agent_config, load_mcp_servers, load_scheduler_jobs
from mcp_client import MCPManager
from agent import Agent
from wechat import WeChatClient
from wechat_accounts import WeChatAccountManager
from skill_manager import SkillManager
from sticker_utils import StickerSelector
from scheduler import TaskScheduler
from history import get_history_info
from models import (
    ModelConfig,
    list_models, save_selected_model, load_selected_model_name,
    get_model_by_name,
)
from wizard import run_wizard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = os.getcwd()
MODELS_DIR = os.path.join(BASE_DIR, "models")
PROACTIVE_CHAT_MIN_SECONDS = 25 * 60
PROACTIVE_CHAT_MAX_SECONDS = 90 * 60
PROACTIVE_CHAT_IDLE_SECONDS = 12 * 60

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
WECHAT_REPLY_INSTRUCTION = (
    "微信回复可以像真人聊天一样分成多条短消息。"
    "简单问题一条即可；情绪复杂、信息较多、需要转折或补充时，"
    "用换行分隔多条消息。每条都必须是可直接发送给对方的内容。"
)
def build_wechat_channel_message(
    sender: str,
    sender_id: str,
    content: str,
    images: list[ImageAttachment] | None = None,
) -> str | list[dict]:
    visible_content = content.strip() or "[图片]"
    text = (
        f'<channel source="wechat" sender="{escape(sender, quote=True)}" '
        f'sender_id="{escape(sender_id, quote=True)}">{escape(visible_content)}</channel>\n'
        f'<delivery>{WECHAT_REPLY_INSTRUCTION}</delivery>'
    )
    if images:
        text += "\n<attachment>用户发来图片。请结合图片、当前上下文和你已激活的身份自然回复。</attachment>"
    return build_multimodal_content(text, images or [])


def split_wechat_reply(text: str, max_parts: int = 4, soft_limit: int = 42) -> list[str]:
    """Split an assistant reply into human-ish WeChat message bubbles."""
    text = text.strip()
    if not text:
        return []

    explicit_parts = [line.strip() for line in text.splitlines() if line.strip()]
    if len(explicit_parts) > 1:
        return explicit_parts[:max_parts]

    if len(text) <= soft_limit:
        return [text]

    sentences = re.findall(r".+?(?:[。！？!?…]+|$)", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) <= 1:
        return [text]

    parts = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > soft_limit:
            parts.append(current)
            current = sentence
            if len(parts) >= max_parts - 1:
                continue
        else:
            current += sentence
    if current:
        parts.append(current)
    if len(parts) > max_parts:
        parts = parts[:max_parts - 1] + ["".join(parts[max_parts - 1:])]
    return [p for p in parts if p]


def send_wechat_reply(wechat_client: WeChatClient, sender_id: str, text: str) -> list[str]:
    parts = split_wechat_reply(text)
    for index, part in enumerate(parts):
        if index:
            time.sleep(min(1.2, max(0.35, len(part) / 35)))
        wechat_client.send_reply(sender_id, part)
        print(f"[WeChat ->] {part}")
    return parts


def send_sticker_if_matched(
    wechat_client: WeChatClient,
    sticker_selector: StickerSelector,
    sender_id: str,
    user_text: str,
    assistant_text: str,
    chat_agent: Agent,
) -> str | None:
    sticker_path = sticker_selector.select(
        sender_id=sender_id,
        user_text=user_text,
        assistant_text=assistant_text,
        skill_name=chat_agent.get_persistent_skill(),
    )
    if not sticker_path:
        return None
    try:
        time.sleep(0.5)
        wechat_client.send_image(sender_id, sticker_path)
        print(f"[WeChat sticker ->] {os.path.basename(sticker_path)}")
        return sticker_path
    except Exception as e:
        logger.warning(f"Failed to send sticker '{sticker_path}' to {sender_id}: {e}")
        return None


def print_help():
    print("""
Commands:
  /help                   Show this help message
  /quit / /exit           Exit the agent
  /reset                  Clear conversation history
  /clear                  Clear current conversation context
  /history                Show conversation history info
  /new-model              Add a new model configuration
  /model-list             Scan and list model configurations
  /model <name|number>    Switch to a different model
  /wechat                 Toggle WeChat mode on/off
  /wechat-list            List saved WeChat bot accounts
  /wechat-account         Alias of /wechat-list
  /wechat-new             Scan QR and switch to a new WeChat account
  /wechat-switch <name|account_id|number> Switch current WeChat bot account by name/id/number
  /wechat-delete [target] Delete a saved WeChat bot account and its local history
  /image <path> [prompt]  Send a local image to the agent
  /account                Alias of /wechat-account
  /skills                 List available skills
  /skills show <skill>    Show skill details and loaded resources
  /skills search <query>  Search skills by name, alias, or description
  /skills reload          Reload skills from disk
  /<skill-name> [args]    Run a skill (aliases are supported)
  /bind <skill>           Bind a skill to current account
  /unbind                 Remove skill binding from current account
  /schedule add <id> "<cron>" "<prompt>"
                          Add a cron job (5-field cron expression)
  /schedule remove <id>   Remove a cron job
  /schedule list          List all scheduled jobs
  <any other text>        Send message to the agent
""")


def format_help_text(skill_manager: SkillManager | None = None) -> str:
    lines = [
        "Commands:",
        "/help - show this help",
        "/new-model - add a new model configuration",
        "/model-list - list models",
        "/model <name|number> - switch model",
        "/reset - reset conversation",
        "/clear - clear current conversation context",
        "/wechat - toggle WeChat mode",
        "/wechat-list - list WeChat bot accounts",
        "/wechat-account - alias of /wechat-list",
        "/wechat-new - scan and switch account",
        "/wechat-switch <name|account_id|number> - switch WeChat bot account by name/id/number",
        "/wechat-delete [name|account_id|number] - delete a WeChat bot account",
        "/image <path> [prompt] - send a local image",
        "/account - alias of /wechat-account",
        "/skills - list skills",
        "/skills show <skill> - show skill details",
        "/skills search <query> - search skills",
        "/bind <skill> - bind skill to current account",
        "/unbind - remove skill binding",
    ]
    if skill_manager:
        skills = skill_manager.list_skills()
        if skills:
            lines.append("")
            lines.append("Skills:")
            for s in skills:
                hint = f" {s['argument_hint']}" if s.get("argument_hint") else ""
                aliases = s.get("aliases") or []
                alias_text = f" (aliases: {', '.join(aliases[:3])})" if aliases else ""
                lines.append(f"/{s['name']}{hint} - {s['description']}{alias_text}")
    return "\n".join(lines)


def handle_schedule_command(cmd: str, scheduler: TaskScheduler) -> None:
    parts = cmd.strip().split(None, 2)
    if len(parts) < 2:
        print("Usage: /schedule add/remove/list ...")
        return

    subcmd = parts[1].lower()
    if subcmd == "list":
        jobs = scheduler.list_jobs()
        if not jobs:
            print("No scheduled jobs.")
            return
        for job in jobs:
            print(f"  [{job['id']}] cron={job['cron']} next={job['next_run']}")
            print(f"    prompt: {job['prompt'][:80]}...")
    elif subcmd == "remove":
        if len(parts) < 3:
            print("Usage: /schedule remove <job_id>")
            return
        job_id = parts[2]
        if scheduler.remove_job(job_id):
            print(f"Removed job '{job_id}'")
        else:
            print(f"Job '{job_id}' not found")
    elif subcmd == "add":
        match = re.match(
            r'schedule\s+add\s+(\S+)\s+"([^"]+)"\s+"([^"]+)"',
            cmd, re.IGNORECASE
        )
        if not match:
            match = re.match(
                r'schedule\s+add\s+(\S+)\s+(\S+)\s+(.+)',
                cmd, re.IGNORECASE
            )
        if not match:
            print('Usage: /schedule add <id> "<cron>" "<prompt>"')
            return
        job_id, cron_expr, prompt = match.groups()
        try:
            scheduler.add_cron_job(job_id, prompt, cron_expr)
            print(f"Added job '{job_id}' with cron '{cron_expr}'")
        except Exception as e:
            print(f"Failed to add job: {e}")
    else:
        print(f"Unknown schedule subcommand: {subcmd}")


def resolve_model_selector(selector: str) -> ModelConfig | None:
    models = list_models(MODELS_DIR)
    selector = selector.strip()
    if not selector:
        return None
    if selector.isdigit():
        index = int(selector)
        if 1 <= index <= len(models):
            return models[index - 1]
    lowered = selector.lower()
    for model in models:
        if model.display_name.lower() == lowered:
            return model
    for model in models:
        safe_name = model.display_name.replace(" ", "_").lower()
        if safe_name == lowered or safe_name.removesuffix(".json") == lowered.removesuffix(".json"):
            return model
    return get_model_by_name(MODELS_DIR, selector)


def format_model_list_text() -> str:
    models = list_models(MODELS_DIR)
    selected = load_selected_model_name(MODELS_DIR)
    if not models:
        return "No model configurations found. Use /new-model to create one."

    lines = ["Model configurations:"]
    for index, model in enumerate(models, 1):
        marker = " *" if model.display_name == selected else ""
        lines.append(f"  {index}. {model.display_name}{marker}")
        lines.append(f"     Provider: {model.provider_name} ({model.provider})")
        lines.append(f"     Model: {model.model}")
        lines.append(f"     Base URL: {model.base_url}")
    lines.append("")
    lines.append("  * = currently selected")
    lines.append("Use /model <name|number> to switch.")
    return "\n".join(lines)


def print_model_list() -> None:
    print()
    print(format_model_list_text())
    print()


def handle_model_command(cmd: str, agent: Agent) -> None:
    parts = cmd.strip().split(None, 1)
    if len(parts) < 2:
        print_model_list()
        return

    target_name = parts[1]
    cfg = resolve_model_selector(target_name)
    if not cfg:
        print(f"Model '{target_name}' not found. Use /model-list to list available models.")
        return
    agent.switch_model(cfg)
    save_selected_model(MODELS_DIR, cfg.display_name)
    print(f"Switched to model: {cfg.display_name} ({cfg.provider_name} / {cfg.model})")


class ModelSetupSession:
    """Line-by-line model setup for the non-blocking terminal loop."""

    def __init__(self):
        self.provider: str | None = None
        self.provider_name: str | None = None
        self.base_url: str | None = None
        self.model: str | None = None
        self.api_key: str | None = None
        self.display_name: str | None = None
        self.step = "provider"
        self.cancelled = False
        self.finished = False

    def start(self) -> None:
        print("\n=== New model configuration ===")
        print("Type /cancel at any time to cancel.")
        self._print_prompt()

    def consume(self, raw: str) -> ModelConfig | None:
        value = raw.strip()
        if value.lower() == "/cancel":
            self.cancelled = True
            print("New model setup cancelled.")
            return None

        if self.step == "provider":
            choice = value or "1"
            if choice in ("1", "claude", "anthropic"):
                self.provider = "claude"
                self.provider_name = "Anthropic"
                self.base_url = "https://api.anthropic.com"
                self.model = "claude-sonnet-4-6"
            elif choice in ("2", "openai"):
                self.provider = "openai"
                self.provider_name = "OpenAI"
                self.base_url = "https://api.openai.com/v1"
                self.model = "gpt-4o"
            else:
                print("Please enter 1/claude or 2/openai.")
                self._print_prompt()
                return None
            self.step = "provider_name"
        elif self.step == "provider_name":
            if value:
                self.provider_name = value
            self.step = "base_url"
        elif self.step == "base_url":
            if value:
                self.base_url = value
            self.step = "model"
        elif self.step == "model":
            if value:
                self.model = value
            if not self.model:
                print("Model ID is required.")
                self._print_prompt()
                return None
            self.step = "api_key"
        elif self.step == "api_key":
            self.api_key = value
            self.step = "display_name"
        elif self.step == "display_name":
            self.display_name = value or f"{self.provider_name} {self.model}"
            self.finished = True
            return ModelConfig(
                display_name=self.display_name,
                provider=self.provider or "openai",
                base_url=self.base_url or "",
                model=self.model or "",
                api_key=self.api_key or "",
                provider_name=self.provider_name or self.provider or "Provider",
            )

        self._print_prompt()
        return None

    def _print_prompt(self) -> None:
        if self.step == "provider":
            print("\nProvider type:")
            print("  1. Claude / Anthropic-compatible")
            print("  2. OpenAI-compatible")
            print("Choice [1/2]: ", end="", flush=True)
        elif self.step == "provider_name":
            print(f"Provider display name [{self.provider_name}]: ", end="", flush=True)
        elif self.step == "base_url":
            print(f"Base URL [{self.base_url}]: ", end="", flush=True)
        elif self.step == "model":
            print(f"Model ID [{self.model}]: ", end="", flush=True)
        elif self.step == "api_key":
            print("API Key [empty allowed]: ", end="", flush=True)
        elif self.step == "display_name":
            print(f"Configuration name [{self.provider_name} {self.model}]: ", end="", flush=True)


def handle_wechat_command(cmd: str, skill_manager: SkillManager | None = None) -> str:
    """Handle commands received from WeChat. Returns response text or None."""
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()

    if command == "/help":
        return format_help_text(skill_manager)
    elif command == "/model-list" or command == "/model":
        return format_model_list_text()
    elif command == "/new-model":
        return "Please add new models from the terminal with /new-model, so the API key is not sent through WeChat."
    elif command == "/reset":
        return "__RESET__"
    elif command == "/clear":
        return "__CLEAR__"
    elif command == "/skills":
        if not skill_manager:
            return "No skill manager available."
        skills = skill_manager.list_skills()
        if not skills:
            return "No skills found."
        lines = ["Skills:"]
        for s in skills:
            hint = f" {s['argument_hint']}" if s.get("argument_hint") else ""
            aliases = s.get("aliases") or []
            alias_text = f" (aliases: {', '.join(aliases[:3])})" if aliases else ""
            lines.append(f"/{s['name']}{hint} - {s['description']}{alias_text}")
        return "\n".join(lines)
    else:
        return f"Unknown command: {command}\nType /help for available commands."


def parse_image_command(cmd_text: str) -> tuple[str, str]:
    match = re.match(r'image\s+(".*?"|\'.*?\'|\S+)(?:\s+(.*))?$', cmd_text, re.IGNORECASE)
    if not match:
        raise ValueError('Usage: /image "<path>" [prompt]')
    path = match.group(1).strip().strip('"').strip("'")
    prompt = (match.group(2) or "").strip()
    return path, prompt or "请看这张图片并自然回复。"


def clear_account_context(base_dir: str, account_id: str | None) -> None:
    """Clear persisted chat context for one WeChat account without deleting folders."""
    if not account_id:
        return
    history_dir = os.path.abspath(os.path.join(base_dir, "history", account_id))
    allowed_root = os.path.abspath(os.path.join(base_dir, "history"))
    if not history_dir.startswith(allowed_root + os.sep):
        raise RuntimeError(f"Refusing to clear unexpected history path: {history_dir}")
    if not os.path.isdir(history_dir):
        os.makedirs(history_dir, exist_ok=True)
        return
    for root, _, files in os.walk(history_dir):
        if "conversation.json" in files:
            path = os.path.join(root, "conversation.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "messages": []}, f, ensure_ascii=False, indent=2)


def sanitize_history_segment(value: str | None, fallback: str = "wechat-account") -> str:
    raw = (value or "").strip() or fallback
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw)
    safe = re.sub(r"\s+", " ", safe).strip(" .")
    if not safe:
        safe = fallback
    if safe.upper() in _WINDOWS_RESERVED_NAMES:
        safe = f"{safe}_"
    return safe[:80]


def _history_file_for_sender(base_dir: str, account_id: str | None, sender_id: str | None) -> str | None:
    if not account_id or not sender_id:
        return None
    return os.path.join(base_dir, "history", account_id, sender_id, "conversation.json")


def sender_has_context(base_dir: str, account_id: str | None, sender_id: str | None) -> bool:
    path = _history_file_for_sender(base_dir, account_id, sender_id)
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("messages"))
    except Exception:
        return False


def _history_message_count(path: str) -> tuple[int, str]:
    if not os.path.isfile(path):
        return 0, "none"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return len(data.get("messages", [])), data.get("saved_at", "unknown")
    except Exception:
        return 0, "unreadable"


def list_wechat_contexts(base_dir: str, account_id: str) -> list[dict]:
    account_history_dir = os.path.join(base_dir, "history", account_id)
    if not os.path.isdir(account_history_dir):
        return []
    contexts: list[dict] = []
    for entry in os.listdir(account_history_dir):
        sender_dir = os.path.join(account_history_dir, entry)
        if not os.path.isdir(sender_dir):
            continue
        count, saved_at = _history_message_count(os.path.join(sender_dir, "conversation.json"))
        if count <= 0:
            continue
        contexts.append({
            "sender_id": entry,
            "messages": count,
            "saved_at": saved_at,
        })
    contexts.sort(key=lambda item: item["saved_at"], reverse=True)
    return contexts


def _print_skill_rows(skills: list[dict]) -> None:
    if not skills:
        print("No skills found. Add SKILL.md files to skills/ or .claude/skills/")
        return
    print("\nAvailable skills:")
    for s in skills:
        hint = f" {s['argument_hint']}" if s.get("argument_hint") else ""
        aliases = s.get("aliases") or []
        alias_text = f"  aliases: {', '.join(aliases[:5])}" if aliases else ""
        resources = s.get("resource_count", 0)
        resource_text = f"  resources: {resources}" if resources else ""
        extras = " |".join(x for x in (alias_text, resource_text) if x)
        extras = f"\n    {extras.strip()}" if extras else ""
        print(f"  /{s['name']}{hint} - {s['description']}{extras}")
    print()


def _print_skill_detail(detail: dict | None) -> None:
    if not detail:
        print("Skill not found.")
        return
    print(f"\nSkill: {detail['name']}")
    if detail["description"]:
        print(f"Description: {detail['description']}")
    if detail["argument_hint"]:
        print(f"Argument hint: {detail['argument_hint']}")
    if detail["version"]:
        print(f"Version: {detail['version']}")
    print(f"Directory: {detail['skill_dir']}")
    if detail["aliases"]:
        print(f"Aliases: {', '.join(detail['aliases'])}")
    if detail["allowed_tools"]:
        print(f"Allowed tools: {', '.join(detail['allowed_tools'])}")
    if detail["resources"]:
        print("Loaded resources:")
        for r in detail["resources"]:
            print(f"  - {r['path']} ({r['chars']} chars)")
    else:
        print("Loaded resources: none")
    preview = detail["body_preview"].replace("\n", " ").strip()
    if preview:
        print(f"Preview: {preview[:240]}...")
    print()


def handle_skills_command(cmd: str, skill_manager: SkillManager) -> str | None:
    parts = cmd.strip().split(None, 2)
    if len(parts) == 1:
        _print_skill_rows(skill_manager.list_skills())
        return None

    subcmd = parts[1].lower()
    if subcmd in ("list", "ls"):
        _print_skill_rows(skill_manager.list_skills())
    elif subcmd in ("show", "info", "detail"):
        if len(parts) < 3:
            print("Usage: /skills show <skill-name>")
        else:
            _print_skill_detail(skill_manager.get_skill_detail(parts[2].strip()))
    elif subcmd in ("search", "find"):
        if len(parts) < 3:
            print("Usage: /skills search <query>")
        else:
            _print_skill_rows(skill_manager.search_skills(parts[2].strip()))
    elif subcmd == "reload":
        skill_manager.reload()
        print(f"Reloaded {len(skill_manager.skills)} skill(s).")
    else:
        print(f"Unknown skills subcommand: {subcmd}")
        print("Usage: /skills [list|show <skill>|search <query>|reload]")
    return subcmd


class NonBlockingInput:
    """Read input in a background thread, provide non-blocking get() on main thread."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        while self._running:
            try:
                line = input()
                self._queue.put(line)
            except (EOFError, KeyboardInterrupt):
                self._running = False
                break

    def get(self, timeout=0.5):
        try:
            return self._queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def stop(self):
        self._running = False


def load_default_wechat_account(account_manager: WeChatAccountManager):
    """Load last selected account without prompting."""
    account_data, account_id = account_manager.get_default_account()
    if account_data:
        return account_data, account_id
    print("WeChat: no saved account. Type '/wechat-new' to scan and add one.")
    return None, None


def main():
    global BASE_DIR, MODELS_DIR
    BASE_DIR = os.getcwd()
    MODELS_DIR = os.path.join(BASE_DIR, "models")

    agent_config = load_agent_config()
    mcp_configs = load_mcp_servers()
    job_configs = load_scheduler_jobs()

    mcp_manager = MCPManager()
    mcp_manager.set_work_dir(BASE_DIR)
    if mcp_configs:
        print(f"Connecting to {len(mcp_configs)} MCP server(s)...")
        mcp_manager.connect_all_sync(mcp_configs)
    print(f"MCP: {mcp_manager.get_available_tools_summary()}")

    # WeChat account selection
    account_manager = WeChatAccountManager()
    wechat_client = WeChatClient(BASE_DIR)
    wechat_connected = False
    active_account_id = None

    try:
        account_data, active_account_id = load_default_wechat_account(account_manager)
        if account_data:
            wechat_connected = wechat_client.connect(account_data)
            if wechat_connected:
                print(f"WeChat: Connected default account ({active_account_id})")
            else:
                print("WeChat: 连接失败")
    except (EOFError, KeyboardInterrupt):
        print("\n跳过 WeChat 登录。")

    # Load skills
    skill_manager = SkillManager(BASE_DIR)
    sticker_selector = StickerSelector(BASE_DIR)
    skills = skill_manager.list_skills()
    if skills:
        print(f"Skills: {', '.join(s['name'] for s in skills)}")

    selected_name = load_selected_model_name(MODELS_DIR)
    model_config = None
    if selected_name:
        model_config = get_model_by_name(MODELS_DIR, selected_name)
    if not model_config:
        models = list_models(MODELS_DIR)
        if not models:
            print("\nNo model configured. Let's set one up.")
            model_config = run_wizard(MODELS_DIR)
        else:
            model_config = models[0]
            save_selected_model(MODELS_DIR, model_config.display_name)

    def account_history_key(account_id: str | None) -> str | None:
        if not account_id:
            return None
        saved_key = account_manager.get_account_history_key(account_id)
        if saved_key:
            return saved_key
        accounts = account_manager.list_accounts()
        target = next((a for a in accounts if a.account_id == account_id), None)
        display_name = (target.display_name if target else None) or account_manager.get_account_display_name(account_id) or account_id
        base_key = sanitize_history_segment(display_name, account_id.split("@", 1)[0])
        duplicates = [
            a for a in accounts
            if ((a.display_name or a.account_id).strip() or a.account_id) == display_name
        ]
        if len(duplicates) > 1:
            base_key = f"{base_key}__{account_id.split('@', 1)[0]}"
        account_manager.set_account_history_key(account_id, base_key)
        return base_key

    def migrate_legacy_account_history(account_id: str | None, history_key: str | None) -> None:
        if not account_id or not history_key or account_id == history_key:
            return
        history_root = os.path.abspath(os.path.join(BASE_DIR, "history"))
        legacy_dir = os.path.abspath(os.path.join(history_root, account_id))
        named_dir = os.path.abspath(os.path.join(history_root, history_key))
        if not legacy_dir.startswith(history_root + os.sep) or not named_dir.startswith(history_root + os.sep):
            return
        if not os.path.isdir(legacy_dir) or os.path.exists(named_dir):
            return
        os.makedirs(history_root, exist_ok=True)
        shutil.move(legacy_dir, named_dir)

    def history_scope(sender_id: str | None = None) -> str | None:
        history_key = account_history_key(active_account_id)
        migrate_legacy_account_history(active_account_id, history_key)
        if history_key and sender_id:
            return os.path.join(history_key, sender_id)
        return history_key

    def apply_bound_skill(target_agent: Agent) -> None:
        if not active_account_id:
            return
        bound_skill = account_manager.get_bound_skill(active_account_id)
        if not bound_skill:
            return
        skill_prompt = skill_manager.get_skill_prompt(bound_skill)
        resolved_name = skill_manager.resolve_skill_name(bound_skill)
        if skill_prompt and resolved_name:
            target_agent.set_persistent_skill(skill_prompt, resolved_name)
        else:
            print(f"Warning: Skill '{bound_skill}' bound to account but not found")

    def make_agent(sender_id: str | None = None):
        target = Agent(agent_config, model_config, mcp_manager, BASE_DIR, account_id=history_scope(sender_id))
        apply_bound_skill(target)
        return target

    agent = make_agent()
    sender_agents: dict[str, Agent] = {}

    def get_sender_agent(sender_id: str) -> Agent:
        if sender_id not in sender_agents:
            sender_agents[sender_id] = make_agent(sender_id)
        return sender_agents[sender_id]

    def rebuild_agents(clear_context: bool = False):
        nonlocal agent, sender_agents, last_wechat_sender, pending_wechat_images
        if clear_context:
            clear_account_context(BASE_DIR, history_scope())
        agent = make_agent()
        sender_agents.clear()
        pending_wechat_images.clear()
        last_wechat_sender = None
        return agent

    def switch_to_wechat_account(account_id: str, clear_context: bool = False) -> bool:
        nonlocal active_account_id, wechat_connected, wechat_mode, wechat_client
        was_polling = wechat_mode
        if wechat_mode:
            wechat_client.stop_polling()
            wechat_mode = False

        account_data = account_manager.get_account(account_id)
        if not account_data:
            print(f"WeChat account '{account_id}' not found or has no token.")
            if was_polling and wechat_connected:
                wechat_client.start_polling()
                wechat_mode = True
            return False

        active_account_id = account_id
        account_manager.set_last_account_id(active_account_id)
        wechat_client = WeChatClient(BASE_DIR)
        wechat_connected = wechat_client.connect(account_data)
        rebuild_agents(clear_context=clear_context)

        display_name = account_manager.get_account_display_name(active_account_id) or active_account_id
        if wechat_connected:
            print(f"WeChat: switched to {display_name} ({active_account_id}).")
            if was_polling:
                wechat_client.start_polling()
                wechat_mode = True
                print("WeChat mode ON.")
            return True

        print(f"WeChat: switched account saved, but connection failed: {display_name} ({active_account_id})")
        return False

    def switch_wechat_account():
        nonlocal pending_wechat_account_name
        account_data = account_manager.qr_login()
        if not account_data:
            print("WeChat: account switch cancelled or failed.")
            return

        switch_to_wechat_account(account_data.account_id, clear_context=False)
        pending_wechat_account_name = account_data.account_id
        print(f"给这个微信机器人账号起个名字 [{account_data.account_id}]：", end="", flush=True)

    def format_wechat_account_report() -> str:
        accounts = account_manager.list_accounts()
        if not accounts:
            return "No saved WeChat accounts. Use /wechat-new to scan and save one."
        lines = ["WeChat accounts:"]
        name_counts: dict[str, int] = {}
        for account in accounts:
            display_name = account.display_name or account.account_id
            name_counts[display_name] = name_counts.get(display_name, 0) + 1
        for index, account in enumerate(accounts, 1):
            active_marker = " *active*" if account.account_id == active_account_id else ""
            history_key = account_history_key(account.account_id) or account.account_id
            migrate_legacy_account_history(account.account_id, history_key)
            account_count, account_saved_at = _history_message_count(
                os.path.join(BASE_DIR, "history", history_key, "conversation.json")
            )
            contexts = list_wechat_contexts(BASE_DIR, history_key)
            total_messages = account_count + sum(c["messages"] for c in contexts)
            display_name = account.display_name or account.account_id
            list_name = display_name
            if name_counts.get(display_name, 0) > 1:
                list_name = f"{display_name} ({account.account_id.split('@', 1)[0]})"
            lines.append(
                f"  {index}. {list_name}{active_marker}\n"
                f"     id: {account.account_id}\n"
                f"     skill: {account.skill or '-'}\n"
                f"     context: {total_messages} messages"
            )
            if account_saved_at != "none":
                lines.append(f"     saved: {account_saved_at}")
        lines.append("")
        lines.append("Use /wechat-switch <name|account_id|number> to switch the default WeChat bot account.")
        lines.append("Use /wechat-new to scan and save/switch to a new WeChat bot account.")
        return "\n".join(lines)

    def print_wechat_account_report() -> None:
        print(format_wechat_account_report())

    def switch_current_wechat_account(selector: str = "") -> None:
        if not selector:
            print(format_wechat_account_report())
            return
        account = account_manager.resolve_account_selector(selector)
        if not account:
            print(f"WeChat account '{selector}' not found.")
            print(format_wechat_account_report())
            return
        switch_to_wechat_account(account.account_id, clear_context=False)

    def delete_wechat_account(selector: str = "") -> None:
        nonlocal active_account_id, wechat_connected, wechat_mode, wechat_client, agent, sender_agents, last_wechat_sender
        target = account_manager.resolve_account_selector(selector) if selector else None
        if not target and not selector and active_account_id:
            target = next((a for a in account_manager.list_accounts() if a.account_id == active_account_id), None)
        if not target:
            print("Usage: /wechat-delete <name|account_id|number>")
            print(format_wechat_account_report())
            return

        deleting_active = target.account_id == active_account_id
        if deleting_active and wechat_mode:
            wechat_client.stop_polling()
            wechat_mode = False

        history_key = account_history_key(target.account_id) or target.account_id
        migrate_legacy_account_history(target.account_id, history_key)
        history_root = os.path.abspath(os.path.join(BASE_DIR, "history"))
        history_dir = os.path.abspath(os.path.join(history_root, history_key))
        if history_dir.startswith(history_root + os.sep) and os.path.isdir(history_dir):
            shutil.rmtree(history_dir)

        removed = account_manager.remove_account(target.account_id)
        display_name = target.display_name or target.account_id
        if removed:
            print(f"WeChat account deleted: {display_name} ({target.account_id})")
        else:
            print(f"WeChat account record may already be gone: {display_name} ({target.account_id})")

        if deleting_active:
            active_account_id = None
            wechat_connected = False
            wechat_client = WeChatClient(BASE_DIR)
            sender_agents.clear()
            pending_wechat_images.clear()
            last_wechat_sender = None
            account_data, next_account_id = load_default_wechat_account(account_manager)
            if account_data:
                active_account_id = next_account_id
                wechat_connected = wechat_client.connect(account_data)
                agent = make_agent()
                if wechat_connected:
                    next_name = account_manager.get_account_display_name(active_account_id) or active_account_id
                    print(f"WeChat: switched to next account {next_name} ({active_account_id}).")
            else:
                agent = make_agent()

    def ensure_wechat_mode() -> bool:
        nonlocal wechat_mode
        if not wechat_connected:
            print("WeChat not connected. Type '/wechat-new' to scan and save an account.")
            return False
        if not wechat_mode:
            wechat_client.start_polling()
            wechat_mode = True
            print("WeChat mode ON.")
        return True

    def activate_skill_for_wechat(skill, args: str = "", target_sender: str | None = None):
        nonlocal last_wechat_sender
        if not active_account_id:
            print("No account selected. Use '/wechat-new' to scan and save one first.")
            return
        if not ensure_wechat_mode():
            return

        raw_args = args.strip()
        force_opening = False
        if raw_args.startswith("--open"):
            force_opening = True
            raw_args = raw_args[len("--open"):].strip()

        skill_prompt = skill_manager.get_skill_prompt(skill.name)
        agent.set_persistent_skill(skill_prompt, skill.name)
        for chat_agent in sender_agents.values():
            chat_agent.set_persistent_skill(skill_prompt, skill.name)
        account_manager.bind_skill(active_account_id, skill.name)

        account_data = account_manager.get_account(active_account_id)
        account_default_sender = account_data.user_id if account_data else None
        target_sender = target_sender or last_wechat_sender or account_default_sender
        if not target_sender:
            display_name = account_manager.get_account_display_name(active_account_id) or active_account_id
            print(f"Skill '{skill.name}' bound to WeChat account {display_name} ({active_account_id}). Waiting for incoming WeChat messages.")
            return

        last_wechat_sender = target_sender
        chat_agent = get_sender_agent(target_sender)
        chat_agent.set_persistent_skill(skill_prompt, skill.name)

        if sender_has_context(BASE_DIR, history_scope(), target_sender) and not force_opening:
            print(f"Skill '{skill.name}' is active for WeChat. Existing context found; waiting for user message.")
            return

        opening_prompt = raw_args or (
            "你现在刚刚在微信里主动联系对方，这是一个全新的聊天，没有已有上下文。"
            "请以当前 skill 身份自然自我介绍，"
            "然后询问对方希望你怎么称呼、名字、基本信息和偏好。"
            "请只输出会直接发给对方的微信内容，不要解释你在扮演谁。"
        )
        response = chat_agent.run(build_wechat_channel_message(target_sender, target_sender, opening_prompt))
        if response:
            send_wechat_reply(wechat_client, target_sender, response)
            send_sticker_if_matched(
                wechat_client,
                sticker_selector,
                target_sender,
                opening_prompt,
                response,
                chat_agent,
            )
            mark_wechat_activity()

    def run_local_image_command(cmd_text: str) -> None:
        nonlocal last_wechat_sender
        path, prompt = parse_image_command(cmd_text)
        image = load_image_attachment(path, source="local")
        if wechat_mode and last_wechat_sender:
            chat_agent = get_sender_agent(last_wechat_sender)
            images_for_turn = []
            if pending_wechat_images.get(last_wechat_sender):
                images_for_turn.extend(pending_wechat_images.pop(last_wechat_sender))
            images_for_turn.append(image)
            channel_msg = build_wechat_channel_message(
                last_wechat_sender,
                last_wechat_sender,
                prompt,
                images_for_turn,
            )
            wechat_client.start_typing(last_wechat_sender)
            try:
                response = chat_agent.run(channel_msg)
            finally:
                wechat_client.stop_typing(last_wechat_sender)
            if response:
                send_wechat_reply(wechat_client, last_wechat_sender, response)
                send_sticker_if_matched(
                    wechat_client,
                    sticker_selector,
                    last_wechat_sender,
                    prompt,
                    response,
                    chat_agent,
                )
                mark_wechat_activity(last_wechat_sender, user_inbound=True)
        else:
            content = build_multimodal_content(prompt, [image])
            response = agent.run(content)
            print(f"\n{response}\n")

    def switch_runtime_model(config: ModelConfig, announce: bool = True) -> None:
        nonlocal model_config
        model_config = config
        agent.switch_model(config)
        save_selected_model(MODELS_DIR, config.display_name)
        sender_agents.clear()
        if announce:
            print(f"Switched to model: {config.display_name} ({config.provider_name} / {config.model})")
            print("Cached WeChat sender agents were refreshed for the next message.")

    def save_new_runtime_model(config: ModelConfig) -> None:
        path = config.save(MODELS_DIR)
        print(f"\nSaved: {path}")
        switch_runtime_model(config)

    if active_account_id:
        bound_skill = account_manager.get_bound_skill(active_account_id)
        resolved_name = skill_manager.resolve_skill_name(bound_skill) if bound_skill else None
        if resolved_name:
            print(f"Skill: {resolved_name} (bound to account {active_account_id})")

    scheduler = TaskScheduler(agent_factory=lambda: make_agent())
    for job in job_configs:
        if job.enabled:
            try:
                scheduler.add_cron_job(job.id, job.prompt, job.cron)
            except Exception as e:
                logger.warning(f"Failed to load scheduled job '{job.id}': {e}")
    scheduler.start()

    wechat_mode = False
    last_wechat_sender = None  # Track last active WeChat sender
    pending_wechat_images: dict[str, list[ImageAttachment]] = {}
    last_wechat_activity_at = time.monotonic()
    next_proactive_chat_at = 0.0
    proactive_unanswered_counts: dict[str, int] = {}

    def schedule_next_proactive_chat() -> None:
        nonlocal next_proactive_chat_at
        next_proactive_chat_at = time.monotonic() + random.randint(
            PROACTIVE_CHAT_MIN_SECONDS,
            PROACTIVE_CHAT_MAX_SECONDS,
        )

    def mark_wechat_activity(sender_id: str | None = None, user_inbound: bool = False) -> None:
        nonlocal last_wechat_activity_at
        last_wechat_activity_at = time.monotonic()
        if sender_id and user_inbound:
            proactive_unanswered_counts.pop(sender_id, None)
        schedule_next_proactive_chat()

    def maybe_send_proactive_wechat_message() -> None:
        nonlocal last_wechat_sender
        now = time.monotonic()
        if now < next_proactive_chat_at:
            return
        schedule_next_proactive_chat()
        if not wechat_mode or not wechat_connected or not active_account_id:
            return
        target_sender = last_wechat_sender
        if not target_sender:
            return
        proactive_count = proactive_unanswered_counts.get(target_sender, 0)
        if proactive_count >= 2:
            return
        if now - last_wechat_activity_at < PROACTIVE_CHAT_IDLE_SECONDS:
            return
        chat_agent = get_sender_agent(target_sender)
        if not chat_agent.get_persistent_skill():
            return

        if proactive_count == 0:
            prompt = (
                "现在已经有一段时间没有收到对方消息。"
                "请根据你当前人物的性格、关系状态、最近聊天上下文，判断此刻适合主动找对方说什么。"
                "输出一条自然的微信主动消息，像真人临时想起对方，而不是任务提醒。"
                "可以关心、调侃、邀约、分享一个小念头，具体取决于你的人设。"
                "不要解释你为什么发消息，不要说自己是 AI，不要写旁白。"
            )
        else:
            prompt = (
                "你上一次已经主动联系过对方，但对方还没有回复。"
                "现在是第二次、也是最后一次主动联系。请联系刚才的上下文和上一条主动消息，"
                "用符合当前人物性格的方式稍微撒娇一点、委屈一点，像是在轻轻试探对方还在不在。"
                "不要催得太用力，不要责怪，不要连续追问太多。"
                "请只输出会直接发给对方的微信内容；如果这次对方仍然不回，之后就不要再主动发起。"
            )
        try:
            print(f"[WeChat proactive] sending as {chat_agent.get_persistent_skill()} to {target_sender}")
            wechat_client.start_typing(target_sender)
            response = chat_agent.run(build_wechat_channel_message(target_sender, target_sender, prompt))
        except Exception as e:
            logger.warning(f"Proactive WeChat message failed: {e}")
            return
        finally:
            wechat_client.stop_typing(target_sender)
        if response:
            send_wechat_reply(wechat_client, target_sender, response)
            send_sticker_if_matched(
                wechat_client,
                sticker_selector,
                target_sender,
                prompt,
                response,
                chat_agent,
            )
            proactive_unanswered_counts[target_sender] = proactive_count + 1
            last_wechat_sender = target_sender
            mark_wechat_activity(target_sender)

    schedule_next_proactive_chat()
    non_blocking = NonBlockingInput()
    model_setup_session: ModelSetupSession | None = None
    pending_wechat_account_name: str | None = None

    print(f"Model: {model_config.display_name} ({model_config.provider_name} / {model_config.model})")
    print(f"History: {get_history_info(BASE_DIR)}")
    print("Micro Agent ready. Type '/help' for commands, '/quit' to exit.\n")

    try:
        while True:
            # 1. Check wechat messages (direct API)
            if wechat_mode:
                while True:
                    msg = wechat_client.get_message(block=False)
                    if msg is None:
                        break

                    image_note = f" (+{len(msg.images or [])} image(s))" if msg.images else ""
                    print(f"\n[WeChat: {msg.sender}] {msg.content or '[图片]'}{image_note}")
                    last_wechat_sender = msg.sender_id
                    mark_wechat_activity(msg.sender_id, user_inbound=True)
                    chat_agent = get_sender_agent(msg.sender_id)

                    if msg.media_errors and not msg.images and not msg.content:
                        send_wechat_reply(wechat_client, msg.sender_id, "这张图片暂时读取不了，可以重新发一次。")
                        continue

                    if msg.images and not msg.content.strip():
                        pending_wechat_images.setdefault(msg.sender_id, []).extend(msg.images)
                        print(f"[WeChat] Buffered {len(msg.images)} image(s) from {msg.sender_id}; waiting for next instruction.")
                        continue

                    # Handle WeChat commands
                    if msg.content.strip().startswith("/") and not msg.images:
                        command_text = msg.content.strip()
                        command_name = command_text.split(None, 1)[0].lstrip("/")
                        skill = skill_manager.get_skill(command_name)
                        if skill:
                            skill_args = command_text.split(None, 1)[1] if len(command_text.split(None, 1)) > 1 else ""
                            try:
                                had_context = sender_has_context(BASE_DIR, history_scope(), msg.sender_id)
                                activate_skill_for_wechat(skill, skill_args, msg.sender_id)
                                if had_context:
                                    print(f"[WeChat] Skill '{skill.name}' active; existing context found, waiting for next message.")
                            except Exception as e:
                                send_wechat_reply(wechat_client, msg.sender_id, f"Skill error: {e}")
                        else:
                            if command_name == "new-model":
                                response = "请在终端里使用 /new-model 新增模型，避免 API Key 出现在微信聊天里。"
                            elif command_name in ("wechat-account", "wechat-list"):
                                response = format_wechat_account_report()
                            elif command_name == "wechat-new":
                                response = "请在终端里使用 /wechat-new 扫码新增或切换微信账号。"
                            elif command_name == "wechat-switch":
                                response = "请在终端里使用 /wechat-switch <账号名|账号ID|序号> 切换微信机器人账号。"
                            elif command_name == "wechat-delete":
                                response = "请在终端里使用 /wechat-delete <账号名|账号ID|序号> 删除微信机器人账号，避免误删。"
                            elif command_name == "model-list":
                                response = format_model_list_text()
                            elif command_name == "model":
                                parts = command_text.split(None, 1)
                                if len(parts) < 2:
                                    response = "Usage: /model <name|number>\n\n" + format_model_list_text()
                                else:
                                    target_config = resolve_model_selector(parts[1])
                                    if not target_config:
                                        response = f"Model '{parts[1]}' not found.\n\n{format_model_list_text()}"
                                    else:
                                        switch_runtime_model(target_config, announce=False)
                                        response = f"Switched to model: {target_config.display_name} ({target_config.provider_name} / {target_config.model})"
                            else:
                                response = handle_wechat_command(command_text, skill_manager)
                            if response == "__RESET__":
                                chat_agent.reset()
                                pending_wechat_images.pop(msg.sender_id, None)
                                print("[WeChat] Conversation reset.")
                            elif response == "__CLEAR__":
                                chat_agent.reset()
                                pending_wechat_images.pop(msg.sender_id, None)
                                send_wechat_reply(wechat_client, msg.sender_id, "上下文已清除。")
                                print("[WeChat] Context cleared.")
                            else:
                                send_wechat_reply(wechat_client, msg.sender_id, response)
                        continue

                    # Format as channel message and run through agent
                    images_for_turn = []
                    if pending_wechat_images.get(msg.sender_id):
                        images_for_turn.extend(pending_wechat_images.pop(msg.sender_id))
                    if msg.images:
                        images_for_turn.extend(msg.images)
                    channel_msg = build_wechat_channel_message(
                        msg.sender,
                        msg.sender_id,
                        msg.content,
                        images_for_turn,
                    )
                    try:
                        wechat_client.start_typing(msg.sender_id)
                        response = chat_agent.run(channel_msg)
                        wechat_client.stop_typing(msg.sender_id)
                        if response:
                            send_wechat_reply(wechat_client, msg.sender_id, response)
                            send_sticker_if_matched(
                                wechat_client,
                                sticker_selector,
                                msg.sender_id,
                                msg.content,
                                response,
                                chat_agent,
                            )
                    except Exception as e:
                        wechat_client.stop_typing(msg.sender_id)
                        logger.error(f"WeChat agent error: {e}")
                        send_wechat_reply(wechat_client, msg.sender_id, f"Error: {e}")

                maybe_send_proactive_wechat_message()

            # 2. Check local input (non-blocking)
            user_input = non_blocking.get(timeout=0.5)
            if user_input is None:
                continue
            raw_user_input = user_input.rstrip("\r\n")

            if pending_wechat_account_name:
                name = raw_user_input.strip() or pending_wechat_account_name
                account_manager.set_account_display_name(pending_wechat_account_name, name)
                print(f"WeChat account name saved: {name} ({pending_wechat_account_name})")
                pending_wechat_account_name = None
                continue

            if model_setup_session:
                completed_config = model_setup_session.consume(raw_user_input)
                if model_setup_session.cancelled:
                    model_setup_session = None
                elif completed_config:
                    save_new_runtime_model(completed_config)
                    model_setup_session = None
                continue

            user_input = raw_user_input.strip()

            if not user_input:
                continue

            # 3. Handle slash commands. Non-slash text is always chat content.
            is_command = user_input.startswith("/")
            cmd_text = user_input[1:].strip() if is_command else ""
            cmd_lower = cmd_text.lower()
            first_word = cmd_lower.split()[0] if cmd_lower else ""
            if is_command and cmd_lower in ("quit", "exit"):
                print("Goodbye.")
                break
            elif is_command and cmd_lower == "help":
                print(format_help_text(skill_manager))
            elif is_command and cmd_lower == "reset":
                agent.reset()
                if last_wechat_sender:
                    pending_wechat_images.pop(last_wechat_sender, None)
                print("Conversation reset.")
            elif is_command and cmd_lower == "clear":
                if wechat_mode and last_wechat_sender:
                    chat_agent = get_sender_agent(last_wechat_sender)
                    chat_agent.reset()
                    pending_wechat_images.pop(last_wechat_sender, None)
                    print(f"WeChat context cleared for {last_wechat_sender}.")
                else:
                    agent.reset()
                    print("Context cleared.")
            elif is_command and cmd_lower == "history":
                print(get_history_info(BASE_DIR))
            elif is_command and cmd_lower in ("new-model", "setup"):
                model_setup_session = ModelSetupSession()
                model_setup_session.start()
            elif is_command and cmd_lower == "model-list":
                print_model_list()
            elif is_command and cmd_lower in ("account", "wechat account", "wechat-account", "wechat-list"):
                print_wechat_account_report()
            elif is_command and (cmd_lower == "wechat-new" or cmd_lower == "wechat new"):
                switch_wechat_account()
            elif is_command and (cmd_lower == "wechat-switch" or cmd_lower.startswith("wechat-switch ") or cmd_lower == "wechat switch" or cmd_lower.startswith("wechat switch ")):
                if cmd_lower.startswith("wechat-switch"):
                    selector = cmd_text.split(None, 1)[1] if len(cmd_text.split(None, 1)) > 1 else ""
                else:
                    parts = cmd_text.split(None, 2)
                    selector = parts[2] if len(parts) > 2 else ""
                switch_current_wechat_account(selector)
            elif is_command and (cmd_lower == "wechat-delete" or cmd_lower.startswith("wechat-delete ") or cmd_lower == "wechat delete" or cmd_lower.startswith("wechat delete ")):
                if cmd_lower.startswith("wechat-delete"):
                    selector = cmd_text.split(None, 1)[1] if len(cmd_text.split(None, 1)) > 1 else ""
                else:
                    parts = cmd_text.split(None, 2)
                    selector = parts[2] if len(parts) > 2 else ""
                delete_wechat_account(selector)
            elif is_command and cmd_lower == "wechat":
                if not wechat_connected:
                    print("WeChat not connected. Type '/wechat-new' to scan and save an account.")
                    continue
                wechat_mode = not wechat_mode
                if wechat_mode:
                    wechat_client.start_polling()
                    print("WeChat mode ON. Messages from WeChat will be processed.")
                    print("Type /wechat again to turn off.")
                else:
                    wechat_client.stop_polling()
                    print("WeChat mode OFF.")
            elif is_command and (cmd_lower == "image" or cmd_lower.startswith("image ")):
                try:
                    run_local_image_command(cmd_text)
                except Exception as e:
                    print(f"\nImage error: {e}\n")
            elif is_command and (cmd_lower == "model" or cmd_lower.startswith("model ")):
                parts = cmd_text.split(None, 1)
                if len(parts) < 2:
                    print("Usage: /model <name|number>")
                    print_model_list()
                else:
                    target_config = resolve_model_selector(parts[1])
                    if not target_config:
                        print(f"Model '{parts[1]}' not found.")
                        print_model_list()
                    else:
                        switch_runtime_model(target_config)
            elif is_command and cmd_lower.startswith("bind"):
                parts = cmd_text.split(None, 1)
                if len(parts) < 2:
                    print("Usage: /bind <skill-name>")
                elif not active_account_id:
                    print("No account selected. Use '/wechat-new' to scan and save one first.")
                else:
                    requested_name = parts[1].strip()
                    skill = skill_manager.get_skill(requested_name)
                    if not skill:
                        print(f"Skill '{requested_name}' not found. Use '/skills search <query>' or '/skills' to list available skills.")
                    else:
                        skill_prompt = skill_manager.get_skill_prompt(skill.name)
                        agent.set_persistent_skill(skill_prompt, skill.name)
                        for chat_agent in sender_agents.values():
                            chat_agent.set_persistent_skill(skill_prompt, skill.name)
                        account_manager.bind_skill(active_account_id, skill.name)
                        print(f"Bound skill '{skill.name}' to account '{active_account_id}'")
            elif is_command and cmd_lower == "unbind":
                if not active_account_id:
                    print("No account selected.")
                else:
                    agent.set_persistent_skill(None)
                    for chat_agent in sender_agents.values():
                        chat_agent.set_persistent_skill(None)
                    account_manager.unbind_skill(active_account_id)
                    print(f"Unbound skill from account '{active_account_id}'")
            elif is_command and cmd_lower.startswith("schedule"):
                handle_schedule_command(cmd_text, scheduler)
            elif is_command and (cmd_lower == "skills" or cmd_lower.startswith("skills ")):
                subcmd = handle_skills_command(cmd_text, skill_manager)
                if subcmd == "reload":
                    apply_bound_skill(agent)
                    for chat_agent in sender_agents.values():
                        apply_bound_skill(chat_agent)
            elif is_command and skill_manager.resolve_skill_name(first_word):
                parts = cmd_text.split(None, 1)
                skill_name = parts[0]
                skill_args = parts[1] if len(parts) > 1 else ""
                skill = skill_manager.get_skill(skill_name)
                try:
                    activate_skill_for_wechat(skill, skill_args)
                except Exception as e:
                    print(f"\nSkill activation error: {e}\n")
            elif is_command:
                print(f"Unknown command: /{cmd_text}\nType /help for available commands.")
            else:
                try:
                    if wechat_mode and last_wechat_sender:
                        # Route through WeChat
                        images_for_turn = []
                        if pending_wechat_images.get(last_wechat_sender):
                            images_for_turn.extend(pending_wechat_images.pop(last_wechat_sender))
                        channel_msg = build_wechat_channel_message(
                            last_wechat_sender,
                            last_wechat_sender,
                            user_input,
                            images_for_turn,
                        )
                        chat_agent = get_sender_agent(last_wechat_sender)
                        wechat_client.start_typing(last_wechat_sender)
                        response = chat_agent.run(channel_msg)
                        wechat_client.stop_typing(last_wechat_sender)
                        if response:
                            send_wechat_reply(wechat_client, last_wechat_sender, response)
                            send_sticker_if_matched(
                                wechat_client,
                                sticker_selector,
                                last_wechat_sender,
                                user_input,
                                response,
                                chat_agent,
                            )
                            mark_wechat_activity(last_wechat_sender, user_inbound=True)
                    else:
                        response = agent.run(user_input)
                        print(f"\n{response}\n")
                except Exception as e:
                    if wechat_mode and last_wechat_sender:
                        wechat_client.stop_typing(last_wechat_sender)
                        send_wechat_reply(wechat_client, last_wechat_sender, f"Error: {e}")
                    print(f"\nError: {e}\n")
    finally:
        non_blocking.stop()
        if wechat_mode:
            wechat_client.stop_polling()
        scheduler.stop()
        mcp_manager.disconnect_all_sync()


if __name__ == "__main__":
    main()
