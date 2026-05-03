import json
import os
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    system_prompt: str = "You are a helpful assistant with access to tools via MCP servers. Use tools when they would help answer the user's question. Be concise and direct."
    max_tokens: int = 16000
    thinking: bool = True
    max_tool_rounds: int = 20


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list = field(default_factory=list)
    env: dict = field(default_factory=lambda: None)


@dataclass
class SchedulerJobConfig:
    id: str
    prompt: str
    cron: str
    enabled: bool = True


def load_agent_config(path: str = "config.json") -> AgentConfig:
    defaults = AgentConfig()
    if not os.path.exists(path):
        return defaults
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return AgentConfig(
        system_prompt=data.get("system_prompt", defaults.system_prompt),
        max_tokens=data.get("max_tokens", defaults.max_tokens),
        thinking=data.get("thinking", defaults.thinking),
        max_tool_rounds=data.get("max_tool_rounds", defaults.max_tool_rounds),
    )


def load_mcp_servers(path: str = "mcp_servers.json") -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    servers = []
    for s in data.get("servers", []):
        servers.append(MCPServerConfig(
            name=s["name"],
            command=s["command"],
            args=s.get("args", []),
            env=s.get("env"),
        ))
    return servers


def load_scheduler_jobs(path: str = "scheduler_jobs.json") -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(SchedulerJobConfig(
            id=j["id"],
            prompt=j["prompt"],
            cron=j["cron"],
            enabled=j.get("enabled", True),
        ))
    return jobs
