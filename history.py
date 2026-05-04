import json
import os
from datetime import datetime


def _get_history_path(base_dir: str, account_id: str = None) -> str:
    if account_id:
        return os.path.join(base_dir, "history", account_id, "conversation.json")
    return os.path.join(base_dir, "history", "conversation.json")


def _serialize_content_block(block):
    """将 Anthropic SDK 的 content block 对象序列化为可 JSON 化的 dict。"""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump()
    if hasattr(block, "__dict__"):
        d = {}
        for k, v in block.__dict__.items():
            if not k.startswith("_"):
                try:
                    json.dumps(v)
                    d[k] = v
                except (TypeError, ValueError):
                    d[k] = str(v)
        if "type" not in d:
            d["type"] = getattr(block, "type", "unknown")
        return d
    return {"type": "unknown", "content": str(block)}


def save_history(messages: list, base_dir: str, account_id: str = None) -> None:
    path = _get_history_path(base_dir, account_id)
    history_dir = os.path.dirname(path)
    os.makedirs(history_dir, exist_ok=True)

    serialized = []
    for msg in messages:
        entry = {"role": msg["role"]}
        content = msg.get("content", "")
        if isinstance(content, str):
            entry["content"] = content
        elif isinstance(content, list):
            entry["content"] = [_serialize_content_block(b) for b in content]
        else:
            entry["content"] = str(content)
        serialized.append(entry)

    data = {
        "saved_at": datetime.now().isoformat(),
        "messages": serialized,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_history(base_dir: str, account_id: str = None) -> list:
    path = _get_history_path(base_dir, account_id)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    messages = data.get("messages", [])
    result = []
    for msg in messages:
        entry = {"role": msg["role"]}
        content = msg.get("content", "")
        if isinstance(content, list):
            cleaned = []
            for block in content:
                if isinstance(block, dict):
                    cleaned.append(block)
            entry["content"] = cleaned if cleaned else ""
        else:
            entry["content"] = content
        result.append(entry)
    return result


def clear_history(base_dir: str, account_id: str = None) -> None:
    path = _get_history_path(base_dir, account_id)
    history_dir = os.path.dirname(path)
    os.makedirs(history_dir, exist_ok=True)
    data = {
        "saved_at": datetime.now().isoformat(),
        "messages": [],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_history_info(base_dir: str, account_id: str = None) -> str:
    path = _get_history_path(base_dir, account_id)
    if not os.path.exists(path):
        return "No history found."
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    saved_at = data.get("saved_at", "unknown")
    msg_count = len(data.get("messages", []))
    return f"History: {msg_count} messages, saved at {saved_at}"
