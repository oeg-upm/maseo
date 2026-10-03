import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

# the model parameters config.yaml may set, shared or per provider:
#   temperature, top_p, top_k, min_p, repeat_penalty  - sampling
#   num_ctx      - context window (Ollama only)
#   max_tokens   - reply budget per call, thinking included
#   seed         - fixed sampling seed for reproducible runs
#   reasoning    - low | medium | high (reasoning effort), or for Ollama
#                  also true | false (thinking on/off)
#   timeout      - seconds per API request (openrouter, deepseek)
MODEL_PARAMS = ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
                "num_ctx", "max_tokens", "seed", "reasoning", "timeout")


class PromptValues(dict):
    def __init__(self, agent_key: str, values: Dict[str, Any]):
        super().__init__(values)
        self.agent_key = agent_key

    def __missing__(self, key):
        print(f"Warning: prompt template of '{self.agent_key}' uses "
              f"'{{{key}}}' but no value for it was provided; "
              f"rendered as empty text")
        return ""


class Config:
    def __init__(self, raw: Dict[str, Any], config_dir: Path, domain: str):
        self._raw = raw
        self._config_dir = config_dir
        self.domain = domain

        base = raw["ontology"]["base_uri_template"].format(domain=domain)
        self.base_uri = base if base.endswith("#") else base + "#"

        self.model_provider: str = raw["model"]["provider"].lower().strip()
        self.model_cfg: Dict[str, Any] = raw["model"].get(self.model_provider, {}) or {}
        if "id" not in self.model_cfg:
            raise ValueError(
                f"Model provider '{self.model_provider}' is missing 'id' in config.yaml"
            )
        self.model_id: str = self.model_cfg["id"]
        # the model parameters: model.<name> is the default for every
        # provider, model.<provider>.<name> overrides it for that one
        self.params: Dict[str, Any] = {}
        for name in MODEL_PARAMS:
            value = self.model_cfg.get(name, raw["model"].get(name))
            if value is not None:
                self.params[name] = value
        self.max_tokens = self.params.get("max_tokens")
        self.temperature = self.params.get("temperature")
        self.timeout = self.params.get("timeout", 300)

        dataset = raw.get("dataset", {}) or {}
        cq_dir = self._resolve_path(dataset.get("cq_dir", "./dataset"))
        pattern = dataset.get("file_pattern", "{domain}_cq2onto_cqs.json")
        self.cq_file: Path = cq_dir / pattern.format(domain=domain)
        if not self.cq_file.is_file():
            raise FileNotFoundError(
                f"No competency question file for domain '{domain}': {self.cq_file}"
            )

        run = raw.get("run", {}) or {}
        self.parse_retries: int = int(run.get("parse_retries", 3))
        self.tool_retries: int = int(run.get("tool_retries", 3))
        self.loop_rounds: int = int(run.get("loop_rounds", 3))
        for name in ("tool_retries", "loop_rounds"):
            value = getattr(self, name)
            if not 1 <= value <= 3:
                raise ValueError(f"run.{name} must be between 1 and 3, "
                                 f"got {value}")

        # The requirements stages run in the order CQAtomization,
        # TermExtraction, TermIdentification, TermRefinement, before
        # OntoGeneration and the validation loop. The TermExtraction
        # Agent always runs; the three experiment toggles each switch
        # one of the other stages:
        #   atomic         - the CQAtomization Agent. With atomic: false
        #                    it never runs and every competency question
        #                    passes through unsplit as its own single
        #                    atomic question, so the later stages work
        #                    on the original phrasing.
        #   identification - the TermIdentification Agent (per-question
        #                    filtering).
        #   refinement     - the TermRefinement Agent (batch refinement)
        #                    and the specification prune that belongs
        #                    to it.
        # generation.others decides the optional agents: an agent is
        # invoked only when it is named there. With others: [] neither
        # AxiomGeneration nor TestGeneration runs, and the themis_test
        # tool (which needs the generated tests) is dropped.
        generation = raw.get("generation", {}) or {}
        if "terms" in generation and not generation.get("terms"):
            print("Note: generation.terms is ignored in maseo_atomic; "
                  "the TermExtraction stage always runs")
        self.use_terms: bool = True
        self.run_atomization: bool = bool(generation.get("atomic", True))
        self.run_identification: bool = bool(
            generation.get("identification", True))
        self.run_refinement: bool = bool(generation.get("refinement", True))
        aliases = {"axiom": "axioms", "axioms": "axioms",
                   "test": "tests", "tests": "tests"}
        others, seen = [], set()
        for item in generation.get("others", []) or []:
            key = aliases.get(str(item).strip().lower())
            if key and key not in seen:
                seen.add(key)
                others.append(key)
        self.generation_others: list = others
        self.run_axioms: bool = "axioms" in others
        self.run_tests: bool = "tests" in others
        # the context blocks fed to OntoGeneration and OntoCorrection
        self.generation_inputs: list = ["terms"] + others

        # The experiment mode names the output folder after the agents
        # that ran, so runs with different toggles never overwrite each
        # other: "all" with every toggle on, "only_<agent>" with one,
        # "<agent>_<agent>" with two (in pipeline order, e.g.
        # "atomic_refinement"), and "none" with none.
        enabled = [name for name, on in (
            ("atomic", self.run_atomization),
            ("identification", self.run_identification),
            ("refinement", self.run_refinement)) if on]
        if len(enabled) == 3:
            self.mode: str = "all"
        elif len(enabled) == 1:
            self.mode = f"only_{enabled[0]}"
        else:
            self.mode = "_".join(enabled) or "none"

        # run.output_dir accepts four placeholders, all filled from this
        # config: {domain} is the domain being run, {mode} is the
        # experiment mode above, {provider} is model.provider, and
        # {model_id} is the active provider's model id made
        # filesystem-safe ("deepseek/deepseek-v4-pro" becomes
        # "deepseek_deepseek-v4-pro")
        safe_model_id = re.sub(r"[^A-Za-z0-9._-]+", "_", self.model_id)
        self.output_dir: Path = self._resolve_path(
            run.get("output_dir",
                    "outputs/{mode}/{model_id}/{domain}").format(
                domain=domain,
                mode=self.mode,
                provider=self.model_provider,
                model_id=safe_model_id)
        )

        hermit = raw.get("hermit", {}) or {}
        self.hermit_jar: Path = self._resolve_path(
            hermit.get("jar_path", "./HermiT.jar")
        )

        themis = raw.get("themis", {}) or {}
        self.themis_mode: str = str(themis.get("mode", "api")).lower()
        self.themis_jar: Path = self._resolve_path(
            themis.get("jar_path", "./themis.jar")
        )
        self.themis_endpoint: str = themis.get(
            "endpoint", "https://themis.linkeddata.es/rest/api/results"
        )

        self.prompts: Dict[str, Dict[str, str]] = raw.get("prompts", {})
        # prompts.common holds named text blocks shared by several agents;
        # any '{name}' placeholder in an agent's instructions is replaced
        # by prompts.common.<name> when the agent is built
        common = self.prompts.get("common", {}) or {}
        self.common_blocks: Dict[str, str] = {
            str(k): str(v) for k, v in common.items()}

        # the full CLaRO template list (claro.templates_path) is loaded
        # as the 'claro_templates' block, so the CQAtomization Agent's
        # '{claro_templates}' placeholder expands to all 93 templates
        claro = raw.get("claro", {}) or {}
        self.claro_templates_path: Path = self._resolve_path(
            claro.get("templates_path", "./claro_templates.txt"))
        if "claro_templates" not in self.common_blocks:
            if self.claro_templates_path.is_file():
                with open(self.claro_templates_path,
                          encoding="utf-8") as f:
                    self.common_blocks["claro_templates"] = f.read()
            else:
                print("Warning: CLaRO template file not found: "
                      f"{self.claro_templates_path}; the CQAtomization "
                      "Agent runs without the template list")
                self.common_blocks["claro_templates"] = (
                    "(the template list is unavailable; rewrite each "
                    "question into the simplest template-like form and "
                    "write the template field as the pattern you used)")

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
        return template.format_map(PromptValues(agent_key, kwargs))


def load_config(path: str | os.PathLike, domain: str) -> Config:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(raw, config_dir=config_path.parent, domain=domain)
