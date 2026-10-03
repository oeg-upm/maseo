# MASEO-Atomic

`MASEO-Atomic` is the current version of the framework. Before any ontology is written, the competency questions (CQs) go through a requirements phase: complex CQs are rewritten into atomic CQs following the [CLaRO](https://github.com/mkeet/CLaRO) templates, and the terms they need are extracted, identified and refined into one binding set of classes, properties, individuals and instances. Optional agents turn this set into axioms and Themis tests. The ontology is then generated and corrected in a validation loop driven by five MCP tool servers, and every step is recorded in a provenance ontology.

The illustration of the MASEO theoretical framework:

<img src="image/maseo_theoretical_framework.png" alt="maseo theoretical framework" width="600">


## MASEO-Atomic Structural Overview

The pipeline consists of eight agents; five of them can be switched on or off in `config.yaml` (`generation`, see Experiment toggles):

| Agent | Responsibility | Switch |
|-------|----------------|--------|
| `CQAtomization Agent` | Rewrites each CQ into atomic CQs that instantiate the 93 CLaRO templates (one asked slot, at most one relation), with a recomposition expression back to the original answer; classifies each question as `domain` or `specification` | `generation.atomic` |
| `TermExtraction Agent` | Extracts all possible terms (classes, object properties, data properties) of each atomic CQ, plus the hidden terms R1–R4 (subclasses, unnamed ranges, common superclasses, cardinality classes) | always runs |
| `TermIdentification Agent` | Filters each atomic question's candidates for precision: drops meta-level nouns and copular verbs, keeps properties that link two classes or an entity to a literal, routes named things to the individuals | `generation.identification` |
| `TermRefinement Agent` | Refines the whole batch: identifies the principle classes that are the core concepts, moves others to individuals or instances, merges near-synonyms, keeps spelling consistent | `generation.refinement` |
| `AxiomGeneration Agent` | Generates axioms from the CQs / atomic CQs with the corresponding term mapping | `axioms` in `generation.others` |
| `TestGeneration Agent` | Generates Themis tests from the original CQs / atomic CQs with the term mapping | `tests` in `generation.others` |
| `OntoGeneration Agent` | Generates the RDF/XML ontology; the refined terms are binding, and an individual is emitted as `owl:NamedIndividual` | always runs |
| `OntoCorrection Agent` | Corrects the ontology with the report of a failing tool | always runs |

| Tool (MCP server) | Check | Backed by |
|------|-------|-----------|
| `syntax_check` | well-formed, parseable RDF/XML | [rdflib](https://rdflib.readthedocs.io/en/stable/) |
| `cq_coverage` | every refined term of every CQ exists in the ontology (literal) | — |
| `oops_scan` | no Critical/Important pitfalls | [OOPS!](https://oops.linkeddata.es/) |
| `hermit_consistency` | consistent, no unsatisfiable classes | [HermiT](http://www.hermit-reasoner.com/) |
| `themis_test` | every CQ's generated tests pass (semantic); only when the TestGeneration Agent runs | [Themis](https://themis.linkeddata.es/) (`themis.jar` or REST API, `themis.mode` in `config.yaml`) |

Each tool is its own MCP server in `mcp_servers/`, started over stdio by `tools.py`.


## Workflow

1. **Processing Terms from CQs**: the CQAtomization Agent turns the CQs into atomic CQs; the TermExtraction Agent extracts the candidate terms; the TermIdentification Agent filters them per question; the TermRefinement Agent refines them into the final set of terms. A switched-off agent hands its input through unchanged.

<img src="image/processing_terms.png" alt="processing terms from cqs" width="650">

2. **Axiom & Test Generation**: from the original CQs, the atomic CQs and the final set of terms, the AxiomGeneration Agent generates the axioms and the TestGeneration Agent generates the Themis tests. Both are optional and independent of each other.

<img src="image/axiom_test_generation.png" alt="axiom and test generation" width="650">

3. **OntoGeneration**: the OntoGeneration Agent generates the ontology from the processed requirements (terms, and axioms and tests when generated).
4. **MCP Tool Ontology Verdict Loop**: each round runs every tool over the ontology in the order `syntax_check`, `cq_coverage`, `oops_scan`, `hermit_consistency`, `themis_test`. A tool that fails sends its report to the OntoCorrection Agent and is checked again, up to `run.tool_retries` times. A round that finds nothing ends the loop. A round that found something is followed by a verification pass over every tool; if that pass is clean the loop ends, otherwise the next round starts, up to `run.loop_rounds`. A tool that cannot run (e.g. OOPS! unreachable) is reported as a tool error, and no correction is attempted.

<img src="image/mcp_verdict_loop.png" alt="mcp tool ontology verdict loop" width="650">


### Features

- **CQ atomization** — complex CQs are split into atomic CQs following the CLaRO templates, each with a recomposition expression, so a question whose atomic tests all pass is answerable
- **Staged term processing** — extraction (with hidden terms), identification and refinement produce one binding set of classes, object properties, data properties, individuals and instances
- **Axiom and test generation** — optional agents give OntoGeneration the axioms to model and the validation loop the Themis tests to pass
- **One MCP server per tool** — the five quality checks are independent MCP servers, and the tool reports drive the OntoCorrection Agent
- **Experiment toggles** — every optional agent can be switched off; the enabled agents name the output folder, so experiment arms never overwrite each other
- **Provenance ontology** — every stage, agent call (rejected attempts included), tool check, correction and term lineage is recorded in W3C vocabulary (PROV-O + EARL) in `<domain>_ontology_prov.owl`; every `dc:source` names both the atomic and the original CQ
- **Multiple LLM providers** — OpenRouter, DeepSeek or a local Ollama, with sampling and reasoning settings per provider


### External tools requirements

| Tool | Purpose | Setup |
|------|---------|-------|
| [HermiT Reasoner](http://www.hermit-reasoner.com/) | Logical consistency checking | Bundled as `HermiT.jar` (`hermit.jar_path` in `config.yaml`) |
| [OOPS! REST API](https://oops.linkeddata.es/) | Ontology pitfall detection | No local setup required — uses the public REST endpoint |
| [Themis](https://themis.linkeddata.es/) | CQ test execution | Bundled as `themis.jar` (`themis.jar_path`); it also calls `themis.linkeddata.es`, so internet access is required |
| [CLaRO](https://github.com/mkeet/CLaRO) | CQ templates for the CQAtomization Agent | Bundled as `claro_templates.txt` (`claro.templates_path`) |
| Java (JRE 8+) | Required to run HermiT and Themis | `sudo apt install default-jre` |


## Execution

Requires Python 3.10 or newer, `java` on PATH and internet access (OOPS! and Themis services). The LLM is selected by `model.provider` in `config.yaml`:

| Provider | Setup |
| --- | --- |
| `openrouter` | `OPENROUTER_API_KEY` in the environment (or `model.openrouter.api_key`); the model is `model.openrouter.id` |
| `deepseek` | `DEEPSEEK_API_KEY` (or `model.deepseek.api_key`); the model is `model.deepseek.id` |
| `ollama` | Ollama running with the model pulled (`ollama pull qwen3.6:27b`); the model is `model.ollama.id`, and `model.ollama.host` points to another machine's Ollama |

Install the dependencies once with [Poetry](https://python-poetry.org/) from the repository root, or with pip from this folder:

```bash
sudo apt install default-jre         # optional if java is not installed in the system
poetry install                       # from the repository root
poetry run python src/maseo_atomic/agent_graph.py <domain>   # domain: wine | awo | odrl | swo | vgo | water
# or, with pip
cd src/maseo_atomic
pip install -r requirements.txt
python agent_graph.py <domain>
```


### Configuration

| Key | Description |
| --- | --- |
| `ontology.base_uri_template` | Ontology IRI, `{domain}` is filled in |
| `model.provider` | `openrouter`, `deepseek` or `ollama` |
| `model.<parameter>` | `temperature`, `top_p`, `top_k`, `min_p`, `repeat_penalty`, `num_ctx`, `max_tokens`, `seed`, `reasoning`, `timeout`; repeated under `model.<provider>` to override them for one provider |
| `dataset.cq_dir`, `dataset.file_pattern` | Where the CQ files are |
| `run.output_dir` | Output folder with the placeholders `{mode}`, `{model_id}`, `{provider}`, `{domain}` |
| `run.parse_retries` | Attempts per agent until its reply maps to the real CQs |
| `run.tool_retries`, `run.loop_rounds` | Corrections per tool, and rounds of the validation loop (1–3 each) |
| `generation.*` | The experiment toggles above |
| `hermit.jar_path`, `themis.*`, `claro.templates_path` | The external tools |
| `prompts.<agent>` | Instruction and prompt template of each agent, `prompts.common` for shared blocks |


## Output


| File | Content |
|------|---------|
| `<domain>_cq_terms.json` | the raw term set from the TermExtraction Agent: every CQ with its atomic CQs and their candidate terms |
| `<domain>_cq_refined_terms.json` | the final set of terms: every CQ with its atomic CQs, templates, recomposition and refined terms |
| `<domain>_axiom.json` | the AxiomGeneration Agent's result, keyed by original CQ id |
| `<domain>_tests.json` | the TestGeneration Agent's result, keyed by original CQ id |
| `<domain>_steps.json` | every step, successful and failed: each agent's input and output, each tool check with its full result and report |
| `<domain>_ontology_initial.owl` | the OntoGeneration Agent's untouched output, before the validation loop |
| `<domain>_ontology.owl` | the final ontology after the validation loop |
| `<domain>_ontology_prov.owl` | the ontology with its provenance (PROV-O + EARL, in a separate `<base>_prov#` namespace), rewritten after every correction |
