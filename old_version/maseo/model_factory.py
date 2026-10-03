import os
 
from config import Config
 
def build_model(config: Config):
    provider = config.model_provider
 
    if provider == "ollama":
        from agno.models.ollama import Ollama
        kwargs = {"id": config.model_id}
        if config.model_cfg.get("host"):
            kwargs["host"] = config.model_cfg["host"]
        # Ollama uses options.num_predict / options.temperature
        options = {}
        if config.max_tokens is not None:
            options["num_predict"] = int(config.max_tokens)
        if config.temperature is not None:
            options["temperature"] = float(config.temperature)
        if options:
            kwargs["options"] = options
        return Ollama(**kwargs)
 
    if provider == "deepseek":
        from agno.models.deepseek import DeepSeek
        api_key = config.get_api_key()
        if api_key:
            os.environ["DEEPSEEK_API_KEY"] = api_key
        kwargs = {"id": config.model_id}
        if config.max_tokens is not None:
            kwargs["max_tokens"] = int(config.max_tokens)
        if config.temperature is not None:
            kwargs["temperature"] = float(config.temperature)
        return DeepSeek(**kwargs)
 
    if provider == "openrouter":
        from agno.models.openrouter import OpenRouter
        api_key = config.get_api_key()
        if api_key:
            os.environ["OPENROUTER_API_KEY"] = api_key
        kwargs = {"id": config.model_id}
        if api_key:
            kwargs["api_key"] = api_key
        if config.max_tokens is not None:
            kwargs["max_tokens"] = int(config.max_tokens)
        if config.temperature is not None:
            kwargs["temperature"] = float(config.temperature)
        return OpenRouter(**kwargs)
 
    raise ValueError(
        f"Unsupported model provider: '{provider}'. "
        f"Supported: 'ollama', 'deepseek', 'openrouter'."
    )
 