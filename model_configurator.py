"""Interactive model configuration utility for Micro Agent."""

from __future__ import annotations

import argparse
import getpass
import os
from dataclasses import replace

from models import (
    ModelConfig,
    list_models,
    load_selected_model_name,
    save_selected_model,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "models")


def _safe_file_name(display_name: str) -> str:
    return display_name.replace(" ", "_").lower() + ".json"


def _model_path(config: ModelConfig) -> str:
    return os.path.join(MODELS_DIR, _safe_file_name(config.display_name))


def _print_models(models: list[ModelConfig]) -> None:
    selected = load_selected_model_name(MODELS_DIR)
    if not models:
        print("No model configurations found.")
        return

    print("\nExisting model configurations:")
    for index, model in enumerate(models, 1):
        marker = "*" if model.display_name == selected else " "
        print(f"  {index}. [{marker}] {model.display_name}")
        print(f"      Provider: {model.provider_name} ({model.provider})")
        print(f"      Model:    {model.model}")
        print(f"      Base URL: {model.base_url}")
        print(f"      File:     {_model_path(model)}")
    print("\n  * = currently selected")


def _prompt(default: str | None, label: str, required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""
        print("This field is required.")


def _prompt_secret(default: str | None, label: str) -> str:
    shown_default = "keep existing" if default else "empty"
    value = getpass.getpass(f"{label} [{shown_default}]: ").strip()
    return value if value else (default or "")


def _prompt_provider(default: str | None = None) -> tuple[str, str, str]:
    print("\nProvider type:")
    print("  1. Claude / Anthropic-compatible")
    print("  2. OpenAI-compatible")
    default_choice = "1" if default == "claude" else "2" if default == "openai" else None
    while True:
        choice = _prompt(default_choice, "Choice [1/2]", required=True)
        if choice == "1":
            return "claude", "https://api.anthropic.com", "claude-sonnet-4-6"
        if choice == "2":
            return "openai", "https://api.openai.com/v1", "gpt-4o"
        print("Please enter 1 or 2.")


def _build_config(existing: ModelConfig | None = None) -> ModelConfig:
    provider, default_base, default_model = _prompt_provider(existing.provider if existing else None)

    provider_name_default = (
        existing.provider_name
        if existing
        else ("Anthropic" if provider == "claude" else "OpenAI")
    )
    provider_name = _prompt(provider_name_default, "Provider display name", required=True)
    base_url = _prompt(existing.base_url if existing else default_base, "Base URL", required=True)
    model = _prompt(existing.model if existing else default_model, "Model ID", required=True)
    api_key = _prompt_secret(existing.api_key if existing else None, "API Key")
    display_name_default = existing.display_name if existing else f"{provider_name} {model}"
    display_name = _prompt(display_name_default, "Configuration name", required=True)

    return ModelConfig(
        display_name=display_name,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        provider_name=provider_name,
    )


def _choose_model(models: list[ModelConfig], prompt_text: str) -> ModelConfig | None:
    if not models:
        print("No model configurations available.")
        return None
    _print_models(models)
    while True:
        raw = input(f"\n{prompt_text} [number/name, blank to cancel]: ").strip()
        if not raw:
            return None
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(models):
                return models[index - 1]
        for model in models:
            if model.display_name.lower() == raw.lower():
                return model
        print("Model not found.")


def _save_config(config: ModelConfig, select_after_save: bool = True) -> None:
    path = config.save(MODELS_DIR)
    if select_after_save:
        save_selected_model(MODELS_DIR, config.display_name)
    print(f"\nSaved: {path}")
    if select_after_save:
        print(f"Selected model: {config.display_name}")


def add_model() -> None:
    print("\n=== Add model configuration ===")
    config = _build_config()
    _save_config(config)


def edit_model() -> None:
    models = list_models(MODELS_DIR)
    target = _choose_model(models, "Edit which model?")
    if not target:
        return
    print(f"\n=== Edit model: {target.display_name} ===")
    updated = _build_config(target)
    old_path = _model_path(target)
    _save_config(updated, select_after_save=target.display_name == load_selected_model_name(MODELS_DIR))
    if updated.display_name != target.display_name and os.path.exists(old_path):
        remove_old = input(f"Remove old config file '{old_path}'? [y/N]: ").strip().lower()
        if remove_old == "y":
            os.remove(old_path)
            print("Old config removed.")


def select_model() -> None:
    models = list_models(MODELS_DIR)
    target = _choose_model(models, "Select which model?")
    if not target:
        return
    save_selected_model(MODELS_DIR, target.display_name)
    print(f"Selected model: {target.display_name}")


def duplicate_model() -> None:
    models = list_models(MODELS_DIR)
    target = _choose_model(models, "Duplicate which model?")
    if not target:
        return
    default_name = f"{target.display_name} Copy"
    new_name = _prompt(default_name, "New configuration name", required=True)
    copied = replace(target, display_name=new_name)
    _save_config(copied)


def interactive() -> None:
    os.makedirs(MODELS_DIR, exist_ok=True)
    while True:
        models = list_models(MODELS_DIR)
        _print_models(models)
        print("\nActions:")
        print("  1. Add new model")
        print("  2. Edit existing model")
        print("  3. Select current model")
        print("  4. Duplicate model")
        print("  5. Refresh scan")
        print("  0. Exit")
        choice = input("\nChoice: ").strip()
        if choice == "1":
            add_model()
        elif choice == "2":
            edit_model()
        elif choice == "3":
            select_model()
        elif choice == "4":
            duplicate_model()
        elif choice == "5":
            continue
        elif choice == "0":
            print("Done.")
            return
        else:
            print("Unknown choice.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Micro Agent model files.")
    parser.add_argument("--list", action="store_true", help="scan and list model configurations, then exit")
    args = parser.parse_args()

    os.makedirs(MODELS_DIR, exist_ok=True)
    if args.list:
        _print_models(list_models(MODELS_DIR))
        return
    interactive()


if __name__ == "__main__":
    main()
