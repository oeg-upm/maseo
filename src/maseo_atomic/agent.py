import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from config import Config
from models import (Answer, Atomization, AtomicTermsReply, Axioms,
                    TermMap, Terms, Tests)

PROVIDER_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com",
}

RULE = "=" * 78
SUB = "-" * 78


def show(title: str, body: str) -> None:
    print("\n" + RULE)
    print(f"| {title}")
    print(SUB)
    print(str(body).strip())
    print(RULE + "\n", flush=True)


def split_json(reply: str) -> tuple:
    """Split a model reply into (json_text, reasoning_prose).

    Models often wrap the JSON object in reasoning text, <think> blocks,
    markdown fences or stray trailing braces. Extract the first balanced
    top-level JSON object and return everything around it as prose."""
    text = re.sub(r"<think>.*?</think>", "", str(reply), flags=re.S)
    text = re.sub(r"```[a-zA-Z]*", "", text)
    start = text.find("{")
    if start == -1:
        return text, ""
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                prose = (text[:start] + " " + text[i + 1:]).strip()
                return text[start:i + 1], prose
    return text[start:], text[:start].strip()


def reasoning_setting(value):
    """A reasoning setting from config.yaml: a level such as "high", or
    true/false for thinking on/off."""
    if isinstance(value, str):
        text = value.strip().lower()
        return {"true": True, "false": False}.get(text, text)
    return value


def build_llm(config: Config):
    p = config.params
    provider = config.model_provider
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        # every sampling option goes in one `options` dict, which is what
        # Ollama's API takes (min_p has no field on ChatOllama)
        options = {k: v for k, v in {
            "temperature": p.get("temperature"),
            "top_p": p.get("top_p"),
            "top_k": p.get("top_k"),
            "min_p": p.get("min_p"),
            "repeat_penalty": p.get("repeat_penalty"),
            "num_ctx": p.get("num_ctx"),
            "num_predict": p.get("max_tokens"),
            "seed": p.get("seed"),
        }.items() if v is not None}
        kwargs = {"model": config.model_id}
        if config.model_cfg.get("host"):
            kwargs["base_url"] = config.model_cfg["host"]
        if p.get("reasoning") is not None:
            kwargs["reasoning"] = reasoning_setting(p["reasoning"])
        return ChatOllama(**kwargs).bind(options=options)
    if provider not in PROVIDER_URLS:
        raise ValueError(
            f"Unsupported model provider: '{provider}'. "
            f"Supported: 'ollama', 'deepseek', 'openrouter'."
        )
    from langchain_openai import ChatOpenAI
    kwargs = {"model": config.model_id,
              "base_url": config.model_cfg.get("base_url") or PROVIDER_URLS[provider],
              "api_key": config.get_api_key() or "not-needed",
              "timeout": config.timeout}
    for name in ("temperature", "top_p", "max_tokens", "seed"):
        if p.get(name) is not None:
            kwargs[name] = p[name]
    if provider == "openrouter":
        # OpenRouter passes these through to the model; the OpenAI schema
        # has no field for them, so they travel in the request body
        extra = {k: v for k, v in {
            "top_k": p.get("top_k"),
            "min_p": p.get("min_p"),
            "repetition_penalty": p.get("repeat_penalty"),
        }.items() if v is not None}
        if isinstance(p.get("reasoning"), str):
            extra["reasoning"] = {"effort": p["reasoning"]}
        if extra:
            kwargs["extra_body"] = extra
    return ChatOpenAI(**kwargs)


class Agent:

    def __init__(self, llm, config: Config, key: str, schema, retries: int = 2):
        self.llm = llm
        self.config = config
        self.key = key
        self.schema = schema
        self.retries = retries
        block = config.prompt(key)
        self.name = block["name"]
        system = block["instructions"]
        # expand shared prompt blocks (prompts.common in config.yaml),
        # then the base URI; plain replace keeps the JSON braces intact
        for block_name, text in config.common_blocks.items():
            system = system.replace("{" + block_name + "}", text.rstrip())
        self.system = system.replace("{base_uri}", config.base_uri)
        self.last_prompt = ""
        # the message actually sent on the LAST model call of the last run.
        # It differs from last_prompt on a retry, because run() appends the
        # rejected reply and a repair instruction before calling again, and
        # steps.json must show what was really sent, not the first draft.
        self.last_sent = ""
        # a note about something the pipeline silently corrected in the
        # accepted reply (currently: an id stamped onto a mis-named entry).
        # Rides along on that call's step record instead of adding one.
        self.last_note = ""
        # the model's reasoning (thinking) on the accepted reply of the
        # last run, when the provider returns it; recorded next to the
        # reply in steps.json
        self.last_thinking = ""
        # one entry per failed model call of the LAST run - an empty
        # reply, a schema-invalid reply, or a provider error - drained
        # into steps.json by the pipeline (log_agent_failures)
        self.failures: list = []

    @staticmethod
    def thinking_text(message) -> str:
        """The reasoning a provider returns beside the reply: Ollama and
        DeepSeek use reasoning_content, OpenRouter uses reasoning."""
        extra = getattr(message, "additional_kwargs", None) or {}
        return str(extra.get("reasoning_content") or extra.get("reasoning")
                   or "")

    @staticmethod
    def reply_text(message) -> str:
        """The text of a model reply. Falls back to the reasoning stream
        when a reasoning model leaves the regular content empty."""
        reply = message.content
        if isinstance(reply, list):
            reply = "".join(b.get("text", "") if isinstance(b, dict)
                            else str(b) for b in reply)
        reply = str(reply or "")
        if not reply.strip():
            extra = getattr(message, "additional_kwargs", None) or {}
            reply = str(extra.get("reasoning_content")
                        or extra.get("reasoning") or "")
        return reply

    def run(self, **values):
        user = self.config.render_prompt(self.key, **values)
        self.last_prompt = user
        show(f"{self.name} | input", user)
        messages = [SystemMessage(content=self.system),
                    HumanMessage(content=user)]
        error = None
        self.failures = []
        self.last_sent = user
        self.last_note = ""
        self.last_thinking = ""
        for attempt in range(self.retries + 1):
            print(f"[{self.name}] calling model "
                  f"(try {attempt + 1}/{self.retries + 1})")
            # what this particular call sends: the original prompt on the
            # first try, the repair instruction on every retry
            sent = str(messages[-1].content)
            self.last_sent = sent
            try:
                message = self.llm.invoke(messages)
            except Exception as e:
                # the provider call itself failed; recorded so the
                # failed call reaches steps.json before the error
                # propagates to the pipeline
                self.failures.append({
                    "attempt": attempt + 1,
                    "error": f"Model call failed: {e}"[:2000],
                    "reply": "", "prompt": sent})
                raise
            reply = self.reply_text(message)
            thinking = self.thinking_text(message)
            finish = (getattr(message, "response_metadata", None)
                      or {}).get("finish_reason")
            show(f"{self.name} | output "
                 f"(try {attempt + 1}/{self.retries + 1})",
                 reply or "(empty reply)")
            if not reply.strip():
                error = "Model returned an empty reply" + (
                    f" (finish_reason={finish})" if finish else "")
                if finish == "length":
                    error += (" - the token budget ran out before any "
                              "answer was written; raise model.max_tokens "
                              "in config.yaml")
                show(f"{self.name} | empty reply "
                     f"(try {attempt + 1}/{self.retries + 1})", error)
                self.failures.append({"attempt": attempt + 1,
                                      "error": error, "reply": "",
                                      "prompt": sent, "thinking": thinking})
                messages = messages + [HumanMessage(content=(
                    "Your previous reply was empty. Do not think out loud. "
                    "Return ONLY the complete JSON object now, no markdown "
                    "fences, no commentary."))]
                continue
            candidate, prose = split_json(reply)
            try:
                parsed = self.schema.model_validate_json(candidate)
                if prose and hasattr(parsed, "reason") and not parsed.reason:
                    parsed.reason = re.sub(r"\s+", " ", prose)[:2000]
                self.last_thinking = thinking
                return parsed
            except ValidationError as e:
                error = str(e)[:2000]
            show(f"{self.name} | validation error "
                 f"(try {attempt + 1}/{self.retries + 1})", error)
            self.failures.append({"attempt": attempt + 1, "error": error,
                                  "reply": str(reply)[:20000],
                                  "prompt": sent, "thinking": thinking})
            messages = messages + [
                AIMessage(content=str(reply)[:20000]),
                HumanMessage(content=(
                    f"Your reply failed validation:\n{error}\n"
                    "Return the corrected COMPLETE JSON object only, "
                    "no markdown fences, no commentary."))]
        raise RuntimeError(f"{self.name} gave no schema-valid reply: {error}")


def create_agents(llm, config: Config) -> dict:
    return {
        "cq_atomization": Agent(llm, config, "cq_atomization", Atomization),
        "term_extraction": Agent(llm, config, "term_extraction",
                                 AtomicTermsReply),
        "term_identification": Agent(llm, config, "term_identification",
                                     TermMap),
        "term_refinement": Agent(llm, config, "term_refinement", TermMap),
        "axiom_generation": Agent(llm, config, "axiom_generation", Axioms),
        "test_generation": Agent(llm, config, "test_generation", Tests),
        "onto_generation": Agent(llm, config, "onto_generation", Answer),
        "onto_correction": Agent(llm, config, "onto_correction", Answer),
    }
