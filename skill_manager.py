"""Skill system - Claude Code compatible SKILL.md format."""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None

logger = logging.getLogger(__name__)

MAX_RESOURCE_CHARS = 60000
RESOURCE_EXTENSIONS = {".md", ".txt", ".json"}
RESOURCE_PRIORITY = {
    "persona.md": 0,
    "memory.md": 1,
    "examples.md": 2,
    "meta.json": 3,
}

# Patterns that indicate meta-commentary (not character dialogue)
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
            # This line is meta-commentary, skip it
            continue
        clean_lines.append(line)
    result = "\n".join(clean_lines).strip()
    return result if result else text.strip()


@dataclass
class Skill:
    name: str
    description: str
    body: str  # SKILL.md content (instructions for the LLM)
    skill_dir: str  # directory containing SKILL.md
    argument_hint: str = ""
    version: str = ""
    user_invocable: bool = True
    allowed_tools: list = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md. Returns (metadata, body)."""
    if not content.startswith("---"):
        return {}, content

    # Find closing ---
    match = re.search(r"\n---\s*(?:\n|$)", content[3:])
    end = match.start() + 3 if match else -1
    if end == -1:
        return {}, content

    fm_text = content[3:end].strip()
    body = content[end + 4:].strip()

    if yaml is not None:
        parsed = yaml.safe_load(fm_text) or {}
        if isinstance(parsed, dict):
            return parsed, body

    # Fallback parser for simple YAML frontmatter.
    meta = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key == "user-invocable":
                meta[key] = value.lower() == "true"
            elif key == "allowed-tools":
                value = value.strip("[]")
                meta[key] = [t.strip().strip('"').strip("'") for t in value.split(",") if t.strip()]
            else:
                meta[key] = value

    return meta, body


def _normalize_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _read_text_file(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if len(text) > MAX_RESOURCE_CHARS:
        return text[:MAX_RESOURCE_CHARS] + "\n\n...[resource truncated]..."
    return text


def _load_meta_json(skill_dir: Path) -> dict:
    meta_path = skill_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to load skill meta.json '{meta_path}': {e}")
        return {}


class SkillManager:
    def __init__(self, base_dir: str = "."):
        self.base_dir = base_dir
        self.skills: dict[str, Skill] = {}
        self.aliases: dict[str, str] = {}
        self._load_skills()

    def _load_skills(self):
        """Recursively scan skills/ and .claude/skills/ for SKILL.md files."""
        scan_dirs = [
            os.path.join(self.base_dir, "skills"),
            os.path.join(self.base_dir, ".claude", "skills"),
        ]

        for skills_dir in scan_dirs:
            if not os.path.isdir(skills_dir):
                continue
            for root, dirs, files in os.walk(skills_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
                if "SKILL.md" in files:
                    skill_path = os.path.join(root, "SKILL.md")
                    name = os.path.basename(root)
                    if name not in self.skills and name not in self.aliases:
                        self._load_skill(skill_path, name)

        logger.info(f"Loaded {len(self.skills)} skill(s)")

    def _load_skill(self, skill_path: str, entry: str):
        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()
            meta, body = _parse_frontmatter(content)

            name = meta.get("name", entry)
            skill_dir = Path(os.path.dirname(skill_path))
            meta_json = _load_meta_json(skill_dir)

            # Replace ${CLAUDE_SKILL_DIR} with actual path in body
            body = body.replace("${CLAUDE_SKILL_DIR}", str(skill_dir))
            body = body.replace("$CLAUDE_SKILL_DIR", str(skill_dir))

            aliases = self._build_aliases(name, entry, meta, meta_json)
            resources = self._load_resources(skill_dir)

            skill = Skill(
                name=name,
                description=meta.get("description", ""),
                body=body,
                skill_dir=str(skill_dir),
                argument_hint=meta.get("argument-hint", ""),
                version=meta.get("version", ""),
                user_invocable=meta.get("user-invocable", True),
                allowed_tools=meta.get("allowed-tools", []),
                aliases=aliases,
                resources=resources,
            )
            self.skills[name] = skill
            self._register_aliases(skill)
            logger.info(f"Loaded skill: {name}")
        except Exception as e:
            logger.warning(f"Failed to load skill '{entry}': {e}")

        logger.info(f"Loaded {len(self.skills)} skill(s)")

    def _build_aliases(self, name: str, entry: str, meta: dict, meta_json: dict) -> list[str]:
        raw_aliases = [
            name,
            entry,
            meta.get("slug", ""),
            meta.get("alias", ""),
            meta.get("aliases", []),
            meta_json.get("slug", ""),
            meta_json.get("english_name", ""),
        ]
        aliases = []
        for item in raw_aliases:
            if isinstance(item, list):
                aliases.extend(str(x) for x in item)
            elif item:
                aliases.append(str(item))
        normalized = []
        for alias in aliases:
            value = _normalize_name(alias)
            if value and value not in normalized:
                normalized.append(value)
        return normalized

    def _register_aliases(self, skill: Skill) -> None:
        for alias in skill.aliases:
            existing = self.aliases.get(alias)
            if existing and existing != skill.name:
                logger.warning(f"Skill alias collision: '{alias}' maps to both '{existing}' and '{skill.name}'")
                continue
            self.aliases[alias] = skill.name

    def _load_resources(self, skill_dir: Path) -> list[dict]:
        resources = []
        for path in skill_dir.rglob("*"):
            if not path.is_file() or path.name == "SKILL.md" or path.name.startswith("."):
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(skill_dir).parts):
                continue
            if path.suffix.lower() not in RESOURCE_EXTENSIONS:
                continue
            try:
                rel_path = path.relative_to(skill_dir).as_posix()
                resources.append({
                    "path": rel_path,
                    "content": _read_text_file(path).replace("${CLAUDE_SKILL_DIR}", str(skill_dir)),
                })
            except Exception as e:
                logger.warning(f"Failed to load skill resource '{path}': {e}")
        resources.sort(key=lambda r: (RESOURCE_PRIORITY.get(os.path.basename(r["path"]).lower(), 50), r["path"]))
        return resources

    def reload(self):
        self.skills.clear()
        self.aliases.clear()
        self._load_skills()

    def get_skill(self, name: str) -> Skill | None:
        resolved = self.resolve_skill_name(name)
        return self.skills.get(resolved) if resolved else None

    def resolve_skill_name(self, name: str) -> str | None:
        if not name:
            return None
        if name in self.skills:
            return name
        return self.aliases.get(_normalize_name(name))

    def get_skill_prompt(self, name: str) -> str | None:
        """Build the system prompt for a skill without executing it."""
        skill = self.get_skill(name)
        if not skill:
            return None
        resources = self._format_resources(skill)
        return (
            f"## 你现在是 {skill.name}\n\n"
            f"请严格遵循以下角色设定来回复用户。"
            f"不要说'我没有这个技能'或'我是AI助手'之类的话。"
            f"你就是这个角色本身，用角色的语气、性格和说话方式来回复。\n\n"
            f"## 输出格式（违反则失败）\n"
            f"你只能输出角色的台词。不能输出任何其他内容。\n\n"
            f"正确示例：\n"
            f"用户：你在干嘛\n"
            f"你：躺着呢，怎么了\n\n"
            f"禁止输出的格式（出现任何一个都算失败）：\n"
            f"- 我已经按照xxx的人设回复了...\n"
            f"- 用xxx的方式来表达...\n"
            f"- 保持了角色xxx的特点\n"
            f"- 现在等待用户回复\n"
            f"- （括号里的动作描写或心理描写）\n"
            f"- 任何解释、分析、总结、旁白\n"
            f"- 任何以'嗯'、'好的'、'已'开头的确认语\n\n"
            f"规则：你的输出将直接作为消息发送给用户。"
            f"如果你输出了任何非角色台词的内容，用户会看到奇怪的对话。"
            f"所以绝对只能输出角色会说的话。"
            f"如果正在微信聊天，简单问题一条回复即可；复杂、暧昧、情绪转折或需要补充时，"
            f"可以用换行拆成多条短消息，每一行都会作为一条独立微信发送。\n\n"
            f"## 角色设定\n\n"
            f"{skill.body}"
            f"{resources}"
        )

    def _format_resources(self, skill: Skill) -> str:
        if not skill.resources:
            return ""
        sections = ["\n\n## 技能目录附加资料\n"]
        for resource in skill.resources:
            sections.append(f"\n### {resource['path']}\n\n{resource['content']}")
        return "\n".join(sections)

    def list_skills(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "argument_hint": s.argument_hint,
                "version": s.version,
                "aliases": [a for a in s.aliases if a != _normalize_name(s.name)],
                "resource_count": len(s.resources),
                "allowed_tools": s.allowed_tools,
            }
            for s in self.skills.values()
            if s.user_invocable
        ]

    def search_skills(self, query: str) -> list[dict]:
        query_norm = _normalize_name(query)
        words = [w for w in re.split(r"\s+", query.lower()) if w]
        matches = []
        for skill in self.skills.values():
            haystack = " ".join([
                skill.name,
                skill.description,
                " ".join(skill.aliases),
                skill.body[:2000],
            ]).lower()
            if query_norm in skill.aliases or all(w in haystack for w in words):
                matches.append(skill)
        return [
            {
                "name": s.name,
                "description": s.description,
                "aliases": [a for a in s.aliases if a != _normalize_name(s.name)],
                "resource_count": len(s.resources),
            }
            for s in matches
            if s.user_invocable
        ]

    def get_skill_detail(self, name: str) -> dict | None:
        skill = self.get_skill(name)
        if not skill:
            return None
        return {
            "name": skill.name,
            "description": skill.description,
            "argument_hint": skill.argument_hint,
            "version": skill.version,
            "skill_dir": skill.skill_dir,
            "aliases": skill.aliases,
            "allowed_tools": skill.allowed_tools,
            "resources": [
                {"path": r["path"], "chars": len(r["content"])}
                for r in skill.resources
            ],
            "body_preview": skill.body[:600],
        }

    def execute(self, skill: Skill, args: str = "", agent=None) -> str:
        """Execute a skill by injecting its body into the agent's system prompt."""
        if agent is None:
            return "Agent not available for skill execution"

        # Clear history so persona instructions aren't diluted by old context
        agent.messages = []

        # Set allowed tools for this skill
        agent.set_allowed_tools(skill.allowed_tools if skill.allowed_tools else None)

        # Use shared prompt builder
        skill_prompt = self.get_skill_prompt(skill.name)
        agent.set_skill_instructions(skill_prompt)

        # Use args as user input, or a greeting prompt
        user_input = args if args else "你好"

        try:
            result = agent.run(user_input)
            return _strip_meta_commentary(result)
        finally:
            # Always clear skill instructions after execution
            agent.clear_skill_instructions()
            agent.set_allowed_tools(None)
