import os
from pathlib import Path
from typing import Any, Dict

import yaml


class Config:
    def __init__(self, raw: Dict[str, Any], config_dir: Path):
        self._raw = raw
        self._config_dir = config_dir

        # Ontology
        self.base_uri: str = raw["ontology"]["base_uri"]

        # Model
        self.model_provider: str = raw["model"]["provider"].lower().strip()
        self.model_cfg: Dict[str, Any] = raw["model"].get(self.model_provider, {}) or {}
        if "id" not in self.model_cfg:
            raise ValueError(
                f"Model provider '{self.model_provider}' is missing 'id' in config.yaml"
            )
        self.model_id: str = self.model_cfg["id"]

        # Shared model settings — per-provider override wins if set
        self.max_tokens: int | None = self.model_cfg.get(
            "max_tokens", raw["model"].get("max_tokens")
        )
        self.temperature: float | None = self.model_cfg.get(
            "temperature", raw["model"].get("temperature")
        )

        # Retries
        self.default_retries: int = int(
            raw.get("agents", {}).get("default_retries", 3)
        )

        # OOPS
        oops_cfg = raw.get("oops", {}) or {}
        self.oops_api_url: str = oops_cfg.get(
            "api_url", "https://oops.linkeddata.es/rest"
        )
        self.oops_request_template: Path = self._resolve_path(
            oops_cfg.get("request_template", "./templates/oops_request_template.xml")
        )

        # Hermit
        hermit_cfg = raw.get("hermit", {}) or {}
        self.hermit_jar: Path = self._resolve_path(
            hermit_cfg.get("jar_path", "./hermit/HermiT.jar")
        )

        # Prompts
        self.prompts: Dict[str, Dict[str, str]] = raw.get("prompts", {})

    def _resolve_path(self, path_str: str) -> Path:
        p = Path(path_str).expanduser()
        if p.is_absolute():
            return p
        return (self._config_dir / p).resolve()

    def get_api_key(self) -> str | None:
        env_var = {
            "deepseek": "DEEPSEEK_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }.get(self.model_provider)
        if env_var is None:
            return None
        return os.environ.get(env_var) or self.model_cfg.get("api_key") or None

    def prompt(self, agent_key: str) -> Dict[str, str]:
        if agent_key not in self.prompts:
            raise KeyError(f"No prompt defined for agent '{agent_key}'")
        return self.prompts[agent_key]

    def render_prompt(self, agent_key: str, **kwargs: Any) -> str:
        block = self.prompt(agent_key)
        template = block.get("prompt_template")
        if not template:
            raise KeyError(f"Agent '{agent_key}' has no prompt_template")
        return template.format(**kwargs)


def load_config(path: str | os.PathLike) -> Config:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(raw, config_dir=config_path.parent)