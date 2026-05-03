from models import ModelConfig, save_selected_model


def run_wizard(models_dir: str) -> ModelConfig:
    print("\n=== Model Setup Wizard ===\n")

    print("Select API provider type:")
    print("  1. Claude (Anthropic API)")
    print("  2. OpenAI (OpenAI-compatible API)")
    while True:
        choice = input("\nChoice [1/2]: ").strip()
        if choice == "1":
            provider = "claude"
            default_base = "https://api.anthropic.com"
            default_model = "claude-sonnet-4-6"
            break
        elif choice == "2":
            provider = "openai"
            default_base = "https://api.openai.com/v1"
            default_model = "gpt-4o"
            break
        print("Please enter 1 or 2.")

    provider_name = input(f"\nProvider name [{('Anthropic' if provider == 'claude' else 'OpenAI')}]: ").strip()
    if not provider_name:
        provider_name = "Anthropic" if provider == "claude" else "OpenAI"

    base_url = input(f"Base URL [{default_base}]: ").strip()
    if not base_url:
        base_url = default_base

    model = input(f"Model ID [{default_model}]: ").strip()
    if not model:
        model = default_model

    api_key = input("API Key: ").strip()
    if not api_key:
        print("Warning: No API key provided. You can set it later by editing the model config file.")

    display_name = input("\nName this model configuration: ").strip()
    if not display_name:
        display_name = f"{provider_name}_{model}".replace(" ", "_")

    config = ModelConfig(
        display_name=display_name,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        provider_name=provider_name,
    )

    path = config.save(models_dir)
    save_selected_model(models_dir, display_name)

    print(f"\nModel '{display_name}' saved to {path}")
    print(f"Provider: {provider_name} ({provider})")
    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print()

    return config
