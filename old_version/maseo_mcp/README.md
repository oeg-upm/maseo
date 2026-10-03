# MASEO-MCP

`MASEO-MCP` implements the framework as an agent-MCP-tool loop: three LLM agents (extraction, generation, correction) are driven by a [LangGraph](https://langchain-ai.github.io/langgraph/) workflow, and every quality check is a tool on an MCP server (`mcp_server.py`) launched over stdio. The agents never call the tools directly: each detector's parsed report is what drives the correction.


## MASEO-MCP Structural Overview

| Agent | Responsibility | Output |
|-------|----------------|--------|
| `Extraction Agent` | Maps every CQ to the ontology terms it needs and generates the Themis verification tests for it, following the official [test catalogue](https://themis.linkeddata.es/tests-info.html) | `<domain>_terms.json`, `<domain>_testsuite.ttl` |
| `Generation Agent` | Drafts the RDF/XML ontology from the CQs and their terms | `<domain>_ontology.owl` |
| `Correction Agent` | Repairs the ontology from its source code, the parsed error message of a detector and the CQs | the corrected ontology |

The agents' output contract lives in `agent.md` as the rule of all agents (it is appended to the system prompts of the generation and correction agents); `config.yaml` (`agents.*`) defines who each agent is.

| Tool | Check | Backed by |
|------|-------|-----------|
| `syntax checker` | well-formed, parseable RDF/XML in the required style | [rdflib](https://rdflib.readthedocs.io/en/stable/) |
| `cq literal coverage` | every mapped CQ term exists (literal) | — |
| `oops pitfall scanner` | no Critical/Important pitfalls | [OOPS!](https://oops.linkeddata.es/) |
| `hermit consistency checker` | consistent, no unsatisfiable classes | [HermiT](https://github.com/phillord/hermit-reasoner) |
| `themis test validator` | every CQ's gold tests pass (semantic) | [Themis](https://github.com/oeg-upm/Themis) (REST API or `themis.jar`, `themis.mode` in `config.yaml`) |

The server also publishes the OOPS! pitfall catalogue (`oops://pitfall-catalogue`) and the provenance format (`cq4oe://provenance-format`) as MCP resources, and the generation and correction prompts as MCP prompts.


## Workflow

1. **Extract**: maps every CQ to the ontology terms it needs *and* generates Themis verification tests for it.
2. **Generate**: drafts the RDF/XML ontology from CQs + terms.
3. **Agent-MCP-Tool Loop**: the five detectors run in order: syntax checker, cq literal coverage, oops pitfall scanner, hermit consistency checker, themis test validator. Each step first passes the ontology source code to the corresponding tool; if it passes, the loop moves on to the next detector; if it fails, the system invokes the correction agent with the `ontology source code`, the `parsed error message` and the `competency questions`, and checks again.
4. **Verify**: finally invokes all five detectors at once, to make sure the generated ontology follows all desired standards. If a check still fails, the loop starts again from the syntax checker (or only the Themis step, when it is the only one failing), up to `run.max_attempts` rounds.



### Features

- **Agent-MCP-tool loop** — every quality check is a tool on one stdio MCP server, so detectors can be added or swapped without touching the agents
- **Literal and semantic CQ coverage** — the mapped terms must exist in the ontology, and every CQ's Themis tests must pass
- **Monotonic correction** — a correction that loses covered CQs without gaining any is rolled back, and the detector tries again from the previous ontology
- **Verification rounds** — the five detectors run together after every round, and the effect of each round is recorded
- **Full traceability** — every agent call, tool call, prompt and ontology snapshot is logged per step and in a complete event trace


### External tools requirements

| Tool | Purpose | Setup |
|------|---------|-------|
| [HermiT Reasoner](http://www.hermit-reasoner.com/) | Logical consistency checking | Bundled as `HermiT.jar` |
| [OOPS! REST API](https://oops.linkeddata.es/) | Ontology pitfall detection | No local setup required — uses the public REST endpoint |
| [Themis](https://themis.linkeddata.es/) | CQ test execution | Bundled as `themis.jar` (`themis.mode: jar`), or the REST API (`themis.mode: api`); both need internet access |
| Java (JRE 8+) | Required to run HermiT and Themis | `sudo apt install default-jre` |


## Execution

Requires Python 3.10, `java` on PATH and internet access (OOPS! and Themis services). LLM provider, model, API key, attempt budget (`run.max_attempts`) and Themis execution mode (`api | jar`) are configured in `config.yaml`.

```bash
sudo apt install default-jre         # optional if java is not installed in the system
cd src/maseo_mcp
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-...
python mcp_client.py <domain>        # domain: wine | awo | odrl | swo | vgo | water
```

The API key is read from `llm.api_key` in `config.yaml`; write `api_key: ${OPENROUTER_API_KEY}` there to take it from the environment (leave it empty for Ollama). A domain is a file `dataset/<domain>_cq2onto_cqs.json`.

| `config.yaml` key | Description |
| --- | --- |
| `llm.provider`, `llm.model` | `openrouter`, `deepseek` or `ollama`, and the model id |
| `llm.api_key`, `llm.base_url` | Key of the provider; `base_url` for another endpoint or a remote Ollama |
| `llm.temperature`, `llm.timeout`, `llm.retries` | Model call settings |
| `dataset.cq_dir`, `dataset.file_pattern` | Where the CQ files are |
| `ontology.base_uri_template` | Ontology IRI, `{domain}` is filled in |
| `themis.mode` | `jar` or `api` |
| `run.max_attempts`, `run.output_dir` | Attempt budget, and the output folder (`outputs/{domain}`) |
| `agents.extraction`, `agents.generation`, `agents.correction` | Instruction of each agent |


## Output

Here is the structure of the output layout of MASEO-MCP; the layout is for each domain (`outputs/<domain>/`).

| File | Content |
|------|---------|
| `<domain>_ontology.owl` | the final RDF/XML ontology |
| `<domain>_terms.json` | per-CQ mapped terms + gold tests |
| `<domain>_testsuite.ttl` | the gold test suite in Turtle |
| `<domain>_tests.json` | every test execution: per-test verdicts, verdict history, sanitizer log |
| `<domain>_steps.json` | one entry per step: agent call, tool call, prompt information and ontology source code snapshot |
| `<domain>_run.json` | structured performance records with before/after effects per correction |
| `<domain>_trace.jsonl` | complete event trace (full prompts, tool calls, results) |
