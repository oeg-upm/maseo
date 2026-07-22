# MASEO: A Multi-Agent System for Explainable Ontology Generation


[![Documentation Status](https://readthedocs.org/projects/maseo/badge/?version=latest)](https://maseo.readthedocs.io/en/latest/?badge=latest)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19052003.svg)](https://doi.org/10.5281/zenodo.19052003) 
[![Project Status: Active – The project has reached a stable, usable state and is being actively developed.](https://www.repostatus.org/badges/latest/active.svg)](https://www.repostatus.org/#active)


This repository provides the artifact for `MASEO`, a research-oriented multi-agent system that automated generate ontologies from competency questions, with a built-in focus on explainability. It aims to make the process of ontology generation more transparent, modular, and intelligent by distributing tasks among specialized agents. Each specialized agent is designed to keep track the logic behind each entity in generated ontology. 


## MASOE Structural Overview

The pipeline consists of four sequential stages:

| Agent | Responsibility | External Tools |
|-------|-------|----------------|
| `Ontology Generation Agent` |  Generates the initial OWL ontology from CQs | None |
| `Syntax Repair Agent` | Fixes RDF/XML syntax errors reported by the parser | [rdflib](https://rdflib.readthedocs.io/en/stable/) |
| `Logical Consistency Agent` | Repairs logical inconsistencies reported by HermiT | [HermiT Reasoner](http://www.hermit-reasoner.com/) |
| `Pitfall Resolution Agent` | Resolves ontology modeling pitfalls reported by OOPS! | [OOPS!](https://oops.linkeddata.es/) |

The illustration of the MASOE framework:

<img src="docs/image/maseo_framework.png" alt="maseo overview" width="500">


### Features

- **End-to-end automation** — from a list of CQs to a validated ontology
- **Role-based agents** — each stage is handled by a dedicated LLM agent with a specific instruction and responsibility
- **Provenance tracking** — every ontology entity carries an append-only `vaem:rationale` log attributed to the agent that made each change, and a `dc:source` log linking each change back to the CQ, pitfall, or error that motivated it

### External tools requirements

| Tool | Purpose | Setup |
|------|---------|-------|
| [HermiT Reasoner](http://www.hermit-reasoner.com/) | Logical consistency checking | Download `HermiT.jar` and update the path in `reason_ontology()` |
| [OOPS! REST API](https://oops.linkeddata.es/) | Ontology pitfall detection | No local setup required — uses the public REST endpoint |
| Java (JRE 8+) | Required to run HermiT | `sudo apt install default-jre` |


## Execution

MASEO support execution over single set of competency questions with sepcific LLM (CLI Execution) as well as batch run over a selection of models and sets of competency questions over various domains (Batch Execution).

### CLI Execution

```bash
python -u cli.py \
    --config       ./config.yaml \
    --cqs_file     ./dataset/cqs/wine_cqs.json \
    --save_file    ./wine.owl \
    --agent_method true
```

| Argument | Required | Description |
| --- | --- | --- |
| `--config` | Yes | Path to `config.yaml`. Defaults to `./config.yaml`. |
| `--cqs_file` | Yes | JSON file with competency questions: `[{"id": "CQ1", "value": "..."}, ...]`. |
| `--save_file` | Yes | Where to write the produced OWL ontology. |
| `--agent_method` | No | `true` (default) runs the full multi-agent pipeline; `false` runs single-pass generation only. |

### Batch Execution

To sweep multiple models and competency-question files in one command, use `run_batch.py`:

```bash
python -u run_batch.py --batch ./batch.yaml
```

`batch.yaml` only contains the list of models that you wish to run.
```yaml
models:
  - provider: openrouter
    id: qwen/qwen3.6-flash
  - provider: deepseek
    id: deepseek-chat
  - provider: ollama
    id: qwen3:32b
  ...
```

Place your competency-question files in `./dataset/cqs/`. For every `(model, cqs_file)` pair the runner invokes MASEO generation (`--agent_method true`) and normal agent generation (`--agent_method false`). All generated ontology and log file will be saved independently.


## Documentation

Additional documentation of the project is available at [readthedocs](https://maseo.readthedocs.io/en/latest/?badge=latest)

- The full document for configuration can be found at: [Configuration](https://maseo.readthedocs.io/en/latest/configuration/)
- The full document for Input file structure can be found at [Input](https://maseo.readthedocs.io/en/latest/input/)
- The full document for Output file structure can be found at [Output](https://maseo.readthedocs.io/en/latest/output/)


# MASEO-MCP Implementation

`src/maseo_mcp` implements the framework as an agent-mcp-tool loop: three LLM agents (extraction, generation, correction) are driven by a LangGraph workflow, and every quality check is a tool on an MCP server (`mcp_server.py`) launched over stdio. 

## Workflow

1. **Extract**: maps every CQ to the ontology terms it needs *and* generates Themis verification tests for it (following the official [test catalogue](https://themis.linkeddata.es/tests-info.html)).
2. **Generate**: drafts the RDF/XML ontology from CQs + terms.
3. **Agent-MCP-Tool Loop**: Here five detectors are implemented: syntax checker, cq literal coverage, oops pitfall scanner, hermit consistency checker, themis test validator (cq semantic coverage). Each step first pass the ontology source code to the corresponding tool, if passed, then move onto the next detecotr; if failed, the system will invoke the correction agent with the input of `ontology source code`, `parsed error message` and `competency questions`
4. **Verify**: Finally invoke all five detectors at once, to finally make sure the generated ontology follows all desired standards.


| Tool | Check | Backed by |
|------|-------|-----------|
| `syntax checker` | well-formed, parseable RDF/XML in the required style | rdflib |
| `cq literal coverage` | every mapped CQ term exists (literal) | — |
| `oops pitfall scanner` | no Critical/Important pitfalls | [OOPS!](https://oops.linkeddata.es/) |
| `hermit consistency checker` | consistent, no unsatisfiable classes | [HermiT](https://github.com/phillord/hermit-reasoner) |
| `themis test validator` | every CQ's gold tests pass (semantic) | [Themis](https://github.com/oeg-upm/Themis) (REST API or `themis.jar`, `themis.mode` in `config.yaml`) |

## Execution

Requires `java` on PATH and internet access (OOPS! and Themis services). LLM provider, prompts, attempt budget (`max_attempts`) and Themis execution mode (`api | jar`) are configured in `config.yaml`; the agents' output contract lives in `agent.md` as the rule of all agents.

```bash
sudo apt install default-jre         # optional if java is not installed in the system
cd src/maseo_mcp
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-...
python mcp_client.py <domain>        # domain: wine | awo | odrl | swo | vgo | water
```

## Output

Here are the structure of the how the output layout of MASEO_MCP, noted that the layout is for each domain.

| File | Content |
|------|---------|
| `<domain>_ontology.owl` | the final RDF/XML ontology |
| `<domain>_terms.json` | per-CQ mapped terms + gold tests |
| `<domain>_testsuite.ttl` | the gold test suite in Turtle |
| `<domain>_tests.json` | every test execution: per-test verdicts, verdict history, sanitizer log |
| `<domain>_steps.json` | one entry per step: agent call, tool call, prompt information and ontology source code snapshot |
| `<domain>_run.json` | structured performance records with before/after effects per correction |
| `<domain>_trace.jsonl` | complete event trace (full prompts, tool calls, results) |

# Acknowledgements

This work was supported by the grant [SOEL: Supporting Ontology Engineering with Large Language Models](https://w3id.org/soel) PID2023-152703NA-I00 funded by MCIN/AEI/10.13039/501100011033 and by ERDF/UE. The authors would also like to thank the EDINT (Espacios de Datos para las Infraestructuras Urbanas Inteligentes) ontology development team for sharing the project resources for evaluation purposes.

