import json
import os
from dataclasses import dataclass, asdict


@dataclass
class ModelConfig:
    display_name: str
    provider: str  # "claude" or "openai"
    base_url: str
    model: str
    api_key: str
    provider_name: str  # user-given name for the provider

    def save(self, models_dir: str) -> str:
        os.makedirs(models_dir, exist_ok=True)
        safe_name = self.display_name.replace(" ", "_").lower()
        path = os.path.join(models_dir, f"{safe_name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        return path


def load_model_config(path: str) -> ModelConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return ModelConfig(**data)


def list_models(models_dir: str) -> list:
    if not os.path.exists(models_dir):
        return []
    models = []
    for fname in os.listdir(models_dir):
        if fname.endswith(".json"):
            path = os.path.join(models_dir, fname)
            try:
                cfg = load_model_config(path)
                models.append(cfg)
            except Exception:
                pass
    return models


def save_selected_model(models_dir: str, display_name: str) -> None:
    state_path = os.path.join(models_dir, ".selected")
    with open(state_path, "w", encoding="utf-8") as f:
        f.write(display_name)


def load_selected_model_name(models_dir: str) -> str | None:
    state_path = os.path.join(models_dir, ".selected")
    if not os.path.exists(state_path):
        return None
    with open(state_path, "r", encoding="utf-8") as f:
        return f.read().strip() or None


def get_model_by_name(models_dir: str, display_name: str) -> ModelConfig | None:
    safe_name = display_name.replace(" ", "_").lower()
    path = os.path.join(models_dir, f"{safe_name}.json")
    if os.path.exists(path):
        return load_model_config(path)
    return None
