import argparse
import asyncio
import json
import os
import re
from typing import Any, Dict, List

from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict

from agent import build_llm, create_agents, show
from config import Config, load_config
from models import (Answer, AtomicCQ, AtomicTermsReply, Atomization, Axioms,
                    CQAtomization, ProvenanceRecorder, SourceEntry, TermMap,
                    Terms, Tests, merge_answers, source_key, stamp_answer)
from tools import MCPToolbox

HERE = os.path.dirname(os.path.abspath(__file__))

TOOLS = [
    ("syntax_check", "RDF/XML syntax errors"),
    ("cq_coverage", "uncovered competency questions"),
    ("oops_scan", "OOPS! pitfalls"),
    ("hermit_consistency", "HermiT logical consistency errors"),
    ("themis_test", "failing Themis competency question tests"),
]


class State(TypedDict, total=False):
    atomization: Atomization        # Step 1: atomic competency questions
    atomic_terms: AtomicTermsReply  # Step 2: terms per atomic question
    identified: TermMap             # the TermIdentification output
    term_map: TermMap               # the latest term mapping (extraction,
                                    # identification or refinement)
    final_document: List[dict]      # the final JSON object of the
                                    # requirements phase
    terms: Terms                    # per-question union for the tools
    axioms: Axioms
    tests: Tests
    answer: Answer
    result: Dict[str, Any]
    tool_index: int
    attempt: int
    round: int
    round_failed: bool
    phase: str          # "loop" (check and correct) or "verify" (check only)
    done: bool
    all_passed: bool


def load_cqs(path) -> list:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [{"id": str(c["id"]), "value": str(c["value"])} for c in data]


def format_cqs(cqs: list) -> str:
    return "\n".join(f"{c['id']}: {c['value']}" for c in cqs)


def normalize_id(value) -> str:
    """The join key of a competency question id: everything that is not a
    letter or a digit is dropped and the rest is lowercased, so "New CQ2",
    "NewCQ2", "new_cq-2" and "NEW CQ 2" all key on "newcq2". An id whose
    text the model reformats therefore still joins onto the real
    question instead of failing validation."""
    return re.sub(r"[^0-9a-z]", "", str(value if value is not None else "").lower())


def canonical_ids(ids) -> Dict[str, str]:
    """Map every normalized key to the one real id that owns it. Two real
    ids that share a key are ambiguous, so neither claims it and both
    keep matching on their exact text only."""
    table: Dict[str, str] = {}
    for real in ids:
        key = normalize_id(real)
        if not key:
            continue
        table[key] = "" if key in table and table[key] != real else real
    return {k: v for k, v in table.items() if v}


def prune_specification_classes(term_map: TermMap,
                                atomization: Atomization) -> list:
    """Deterministic post-pass over the refined terms: remove the classes
    that only the modelling questions coined.

    The TermRefinement prompt asks the model to drop such classes, but a
    ~30B model does not reliably act on a batch-global rule, so this pass
    enforces it in code. A class is KEPT when a domain question uses it,
    or when any atomic mapping that lists it also names an individual or
    instance - a specification question grounded in a named thing (ODRLv2,
    Not, Sequencing) is about a real domain kind, and that kind must stay
    to type the named thing. Every other class - one that appears only in
    specification entries and is anchored to nothing - names the asked
    slot of a how-is-it-expressed question, not a kind of the domain, and
    is removed from every mapping. Returns the sorted removed names."""
    qtype = {e.cq_id: e.question_type for e in atomization.atomization}
    keep = set()
    for entry in term_map.terms:
        domain = qtype.get(entry.cq_id, "domain") != "specification"
        for m in entry.atomic_cq_mapping:
            if domain or m.individuals or m.instances:
                keep.update(m.classes)
    dropped = set()
    for entry in term_map.terms:
        for m in entry.atomic_cq_mapping:
            removed = [c for c in m.classes if c not in keep]
            if removed:
                dropped.update(removed)
                m.classes = [c for c in m.classes if c in keep]
    return sorted(dropped)


def repair_mapping_ids(term_map: TermMap, atomization: Atomization) -> TermMap:
    """Rewrite the atomic ids inside a grouped reply onto their canonical
    spelling. run_validated repairs the id of the entry itself; the ids of
    the atomic questions nested in it are the keys the later joins use, so
    they are normalized the same way - an entry the model returns under
    "NewCQ6_a1" still joins onto "New CQ6_a1"."""
    table = canonical_ids(atomization.atomic_ids())
    for entry in term_map.terms:
        for mapping in entry.atomic_cq_mapping:
            real = table.get(normalize_id(mapping.atomic_cq_id))
            if real and real != mapping.atomic_cq_id:
                mapping.atomic_cq_id = real
    return term_map


def atomization_document(cqs: list, atomization: Atomization) -> list:
    """Every competency question with its atomic questions, as the JSON
    object the TermExtraction Agent receives in one prompt."""
    document = []
    for c in cqs:
        atom = atomization.for_cq(c["id"])
        document.append({
            "cq_id": c["id"], "cq": c["value"],
            "question_type": atom.question_type if atom else "domain",
            "recomposition": atom.recomposition if atom else "",
            "atomic_cqs": [{"atomic_cq_id": a.atomic_cq_id,
                            "atomic_cq": a.atomic_cq,
                            "template": a.template}
                           for a in (atom.atomic_cqs if atom else [])]})
    return document


def build_term_mapping(cqs: list, atomization: Atomization,
                       atomic_terms: AtomicTermsReply) -> list:
    """The TermIdentification input: every original question with its
    atomic questions and the candidate terms of each one."""
    document = []
    for c in cqs:
        atom = atomization.for_cq(c["id"])
        entry = {"cq_id": c["id"], "cq": c["value"],
                 "question_type": atom.question_type if atom else "domain",
                 "atomic_cq_mapping": []}
        for a in (atom.atomic_cqs if atom else []):
            t = atomic_terms.for_atomic(a.atomic_cq_id)
            entry["atomic_cq_mapping"].append({
                "atomic_cq_id": a.atomic_cq_id,
                "atomic_cq": a.atomic_cq,
                "classes": list(t.classes) if t else [],
                "object_properties": list(t.object_properties) if t else [],
                "data_properties": list(t.data_properties) if t else [],
                "individuals": list(t.individuals) if t else [],
                "instances": list(t.instances) if t else []})
        document.append(entry)
    return document


def build_final_document(cqs: list, atomization: Atomization,
                         term_map: TermMap) -> list:
    """The term mapping joined with each question's recomposition and
    each atomic question's CLaRO template. Built for the TermRefinement
    input, and again for the final JSON object that the AxiomGeneration
    and TestGeneration Agents receive."""
    document = []
    for c in cqs:
        atom = atomization.for_cq(c["id"])
        mapping = term_map.for_cq(c["id"])
        entry = {"cq_id": c["id"], "cq": c["value"],
                 "question_type": atom.question_type if atom else "domain",
                 "recomposition": atom.recomposition if atom else "",
                 "atomic_cq_mapping": []}
        for a in (atom.atomic_cqs if atom else []):
            m = None
            if mapping:
                m = next((x for x in mapping.atomic_cq_mapping
                          if x.atomic_cq_id == a.atomic_cq_id), None)
            entry["atomic_cq_mapping"].append({
                "atomic_cq_id": a.atomic_cq_id,
                "atomic_cq": a.atomic_cq,
                "template": a.template,
                "classes": list(m.classes) if m else [],
                "object_properties": list(m.object_properties) if m else [],
                "data_properties": list(m.data_properties) if m else [],
                "individuals": list(m.individuals) if m else [],
                "instances": list(m.instances) if m else []})
        document.append(entry)
    return document


def format_atomic_document(final_document: list) -> str:
    """The OntoGeneration/OntoCorrection view of the requirements: every
    original question with its atomic competency questions and the terms
    of EACH atomic question, so the ontology is designed against the
    operational (atomic) reading of every question rather than against
    the original phrasing with a detached per-question term union.

    A question that was never split - its single atomic question repeats
    the original text, as every question does when CQAtomization is
    toggled off - is shown as one line with its terms, without the
    redundant atomic echo, so the model cites the original id."""
    lines = []
    for e in final_document:
        mapping = e.get("atomic_cq_mapping", [])
        unsplit = (len(mapping) == 1
                   and str(mapping[0].get("atomic_cq", "")).strip()
                   == str(e["cq"]).strip())
        lines.append(f"{e['cq_id']} ({e.get('question_type') or 'domain'}): "
                     f"{e['cq']}")
        recomposition = e.get("recomposition", "")
        if not unsplit and recomposition and recomposition != "identity":
            lines.append(f"  recomposition: {recomposition}")
        for m in mapping:
            if not unsplit:
                lines.append(f"  {m['atomic_cq_id']}: {m['atomic_cq']}")
            parts = []
            for field in ("classes", "object_properties", "data_properties",
                          "individuals", "instances"):
                values = m.get(field) or []
                if values:
                    parts.append(f"{field}: {', '.join(values)}")
            lines.append(("  " if unsplit else "    ") + "terms - "
                         + ("; ".join(parts) if parts else "(none)"))
    return "\n".join(lines)


def format_terms(cqs: list, terms: Terms) -> str:
    lines = []
    for c in cqs:
        t = terms.for_cq(c["id"])
        parts = []
        if t:
            for field in ("classes", "object_properties",
                          "data_properties", "individuals", "instances"):
                values = getattr(t, field)
                if values:
                    parts.append(f"{field}: {', '.join(values)}")
        lines.append(f"{c['id']}: " + ("; ".join(parts) if parts else "(no terms)"))
    return "\n".join(lines)


def format_axioms(cqs: list, axioms: Axioms) -> str:
    lines = []
    for c in cqs:
        a = axioms.for_cq(c["id"])
        lines.append(f"{c['id']}: "
                     + ("; ".join(a.axioms) if a and a.axioms else "(no axioms)"))
    return "\n".join(lines)


def format_tests(cqs: list, tests: Tests) -> str:
    lines = []
    for c in cqs:
        t = tests.for_cq(c["id"])
        lines.append(f"{c['id']}: "
                     + ("; ".join(t.tests) if t and t.tests else "(no tests)"))
    return "\n".join(lines)


class MASEOPipeline:

    def __init__(self, config: Config):
        self.config = config
        self.cqs = load_cqs(config.cq_file)
        self.cq_texts = {c["id"]: c["value"] for c in self.cqs}
        # the live provenance recorder: every step record goes to it the
        # moment it is logged, and the provenance ontology is rendered
        # from what it holds right after each OntoGeneration and
        # OntoCorrection response is saved
        self.prov_recorder = ProvenanceRecorder(
            config.base_uri,
            # one run = one PipelineRun node; domain and experiment mode
            # name it so runs of both arms can be merged and compared
            run_id=f"{config.domain}_{config.mode}",
            # with generation.atomic false nothing was atomized: the
            # identity wrappers must not become an atomic layer
            atomic=config.run_atomization)
        self.agents = create_agents(build_llm(config), config)
        # themis_test verifies the generated tests, so it runs only when
        # the TestGeneration Agent runs (generation.others names tests)
        self.tools = [t for t in TOOLS
                      if t[0] != "themis_test" or config.run_tests]
        # step numbers for the printed progress, shifted by the toggled
        # and optional agents; atomization is Step 1 even when the agent
        # is toggled off, extraction is Step 2
        n = 3
        self.identify_step = n if config.run_identification else 0
        n += 1 if config.run_identification else 0
        self.refine_step = n if config.run_refinement else 0
        n += 1 if config.run_refinement else 0
        self.axiom_step = n if config.run_axioms else 0
        n += 1 if config.run_axioms else 0
        self.test_step = n if config.run_tests else 0
        n += 1 if config.run_tests else 0
        self.onto_step = n
        # every atomic question id -> the id of the original question it
        # came from, filled after atomization; the dc:source expansion
        # and the id -> text table for the OWL serializer build on it
        self.atomic_parent: Dict[str, str] = {}
        self.toolbox = None
        os.makedirs(config.output_dir, exist_ok=True)
        domain = config.domain
        # Three ontology files per run, all three present from the first
        # OntoGeneration response on. The two plain ones are what the
        # validation tools read and what the agents see:
        #   <domain>_ontology_initial.owl - the OntoGeneration Agent's
        #       untouched output, written once before the validation loop
        #   <domain>_ontology.owl - the current ontology, rewritten after
        #       every OntoCorrection, final after the loop
        #   <domain>_ontology_prov.owl - the current ontology with the
        #       provenance recorded so far injected, written right after
        #       the OntoGeneration response and rewritten after every
        #       OntoCorrection response (and once more at the end, when
        #       the last checks are in)
        self.onto_path = str(config.output_dir / f"{domain}_ontology.owl")
        self.onto_initial_path = str(
            config.output_dir / f"{domain}_ontology_initial.owl")
        self.onto_prov_path = str(
            config.output_dir / f"{domain}_ontology_prov.owl")
        # the raw term set from the TermExtraction Agent (Step 2),
        # before any filtering or refinement
        self.cq_terms_path = str(
            config.output_dir / f"{domain}_cq_terms.json")
        # the final JSON object of the requirements phase (Step 4 output);
        # the other intermediate stages write no files of their own -
        # their inputs and outputs are all recorded in steps.json
        self.cq_refined_terms_path = str(
            config.output_dir / f"{domain}_cq_refined_terms.json")
        self.axiom_path = str(config.output_dir / f"{domain}_axiom.json")
        self.tests_path = str(config.output_dir / f"{domain}_tests.json")
        self.steps_path = str(config.output_dir / f"{domain}_steps.json")
        self.steps = []

    def log_step(self, agent: str = "", tool: str = "",
                 error_message: str = "", input_text: str = "",
                 output="", extra: Dict[str, Any] = None) -> None:
        entry = {"agent": agent, "tool": tool,
                 "error_message": error_message,
                 "input": input_text, "output": output}
        # extra carries the pipeline's own position (round, phase,
        # attempt) on tool checks and corrections, so the provenance
        # builder groups validation rounds exactly instead of guessing
        if extra:
            entry.update(extra)
        self.steps.append(entry)
        self.prov_recorder.record(entry)
        self.save_json(self.steps_path, self.steps)

    def log_agent_step(self, key: str, result, tool: str = "",
                       error_message: str = "",
                       extra: Dict[str, Any] = None) -> None:
        agent = self.agents[key]
        # last_sent, not last_prompt: on a retry the accepted reply answered
        # the repair instruction, so that is the input this call really had.
        # last_note carries anything the pipeline corrected in the reply.
        # the model's reasoning, when the provider returned it, rides
        # along with the reply it led to
        if agent.last_thinking:
            extra = dict(extra or {}, thinking=agent.last_thinking)
        self.log_step(agent=agent.name, tool=tool,
                      error_message=error_message or agent.last_note,
                      input_text=agent.last_sent or agent.last_prompt,
                      output=result.model_dump() if result is not None else "",
                      extra=extra)

    def log_agent_failures(self, key: str, tool: str = "") -> None:
        """Every failed model call of the agent's last run - an empty
        reply, a schema-invalid reply, or a provider error - becomes its
        own steps.json entry with error_message filled, so the failed
        calls are recorded next to the successful ones."""
        agent = self.agents[key]
        for failure in getattr(agent, "failures", []):
            self.log_step(agent=agent.name, tool=tool,
                          error_message=(f"model call {failure['attempt']} "
                                         f"failed: {failure['error']}"),
                          input_text=(failure.get("prompt")
                                      or agent.last_prompt),
                          output=failure.get("reply", ""),
                          extra=({"thinking": failure["thinking"]}
                                 if failure.get("thinking") else None))
        agent.failures = []

    def save_json(self, path: str, data) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def serialize(self, answer: Answer) -> str:
        return answer.to_owl_document(self.config.base_uri, self.cq_texts)

    def write_ontology(self, answer: Answer, path: str | None = None) -> None:
        with open(path or self.onto_path, "w", encoding="utf-8") as f:
            f.write(self.serialize(answer))

    def write_final(self, answer: Answer) -> None:
        self.write_ontology(answer)
        self.write_provenance(answer)

    def write_provenance(self, answer: Answer) -> None:
        """<domain>_ontology_prov.owl: the ontology just saved, with the
        provenance recorded so far injected - every agent call so far,
        successful or failed, every tool check and validation round,
        every ontology version and what changed between them. Written
        right after the OntoGeneration Agent's response and rewritten
        after every OntoCorrection Agent's response, so the three files
        exist from the first generation on; the provenance individuals
        live in their own <base>_prov# namespace and the two plain
        ontology files stay untouched."""
        self.prov_recorder.record_version(answer)
        document = self.prov_recorder.inject(self.serialize(answer))
        with open(self.onto_prov_path, "w", encoding="utf-8") as f:
            f.write(document)
        print(f"Provenance ontology updated: {self.onto_prov_path}")

    def cq_input(self, state: State) -> str:
        # OntoGeneration and OntoCorrection read every original question
        # with its atomic questions and the terms of each atomic question;
        # the plain original list is only the fallback for a state that
        # carries no final document
        document = state.get("final_document")
        if document:
            return format_atomic_document(document)
        return format_cqs(self.cqs)

    def build_context(self, state: State) -> str:
        # the extra material offered to OntoGeneration and OntoCorrection,
        # selected by generation.others; the terms travel inline with the
        # atomic questions (cq_input), so a separate terms block is added
        # only on the fallback path where that document is missing
        blocks = []
        if ("terms" in self.config.generation_inputs and state.get("terms")
                and not state.get("final_document")):
            blocks.append("Terms proposed for each competency question:\n"
                          + format_terms(self.cqs, state["terms"]))
        if "axioms" in self.config.generation_inputs and state.get("axioms"):
            blocks.append("Axioms proposed for each competency question:\n"
                          + format_axioms(self.cqs, state["axioms"]))
        if "tests" in self.config.generation_inputs and state.get("tests"):
            blocks.append("Themis tests each competency question must pass:\n"
                          + format_tests(self.cqs, state["tests"]))
        return "\n\n".join(blocks)

    def run_validated(self, key: str, entries_of, ids=None,
                      id_field: str = "cq_id", **kwargs):
        # the reply can be schema valid yet useless, e.g. the model echoing
        # the prompt's example with placeholder ids; accept it only when at
        # least one entry maps to a real id.
        # Ids match on their normalized form, so a question id that carries
        # a space ("New CQ2") still joins when the model writes it back as
        # "NewCQ2" or "new_cq2", and every matched entry is rewritten to the
        # canonical id so that all downstream joins - which compare ids
        # exactly - see one spelling.
        ids = list(ids) if ids is not None else [c["id"] for c in self.cqs]
        exact = set(ids)
        table = canonical_ids(ids)
        name = self.agents[key].name
        for attempt in range(1, self.config.parse_retries + 1):
            # the failed model calls of this run are drained into
            # steps.json whether the run returns or raises
            try:
                result = self.agents[key].run(**kwargs)
            finally:
                self.log_agent_failures(key)
            matched = 0
            for entry in entries_of(result):
                given = getattr(entry, id_field, "")
                if given in exact:
                    matched += 1
                    continue
                real = table.get(normalize_id(given))
                if real:
                    setattr(entry, id_field, real)
                    matched += 1
            if matched:
                return result
            # one question expected, one entry returned: the entry IS that
            # question, whatever the model called it. Models RENAME rather
            # than reformat - "New CQ4" comes back as "CQ4" because the id
            # looks tidier without the prefix - and no normalisation
            # recovers a rename, so the expected id is stamped on instead.
            # Safe only because the caller asked about a single question:
            # there is nothing else the entry could be about.
            entries = list(entries_of(result))
            if len(ids) == 1 and len(entries) == 1:
                given = getattr(entries[0], id_field, "")
                setattr(entries[0], id_field, ids[0])
                print(f"[{name}] entry returned as '{given}'; stamped with "
                      f"the expected id '{ids[0]}'")
                # the reply was accepted but rewritten; the note rides on
                # this call's own step record rather than adding a second one
                self.agents[key].last_note = (
                    f"entry returned as '{given}'; stamped with the "
                    f"expected id '{ids[0]}'")
                return result
            # a schema-valid reply that names no real question is a
            # failed call too: recorded with its parsed output before
            # the agent is retried
            error = (f"parsed reply maps to no real id "
                     f"(attempt {attempt}/{self.config.parse_retries})")
            print(f"[{name}] {error}; retrying the agent")
            self.log_step(agent=name, error_message=error,
                          input_text=self.agents[key].last_prompt,
                          output=result.model_dump())
        raise RuntimeError(f"{name} produced no entry matching the "
                           "expected ids after "
                           f"{self.config.parse_retries} attempts")

    def atomize_cqs(self, state: State) -> State:
        atomization = Atomization(atomization=[])
        if not self.config.run_atomization:
            # the without-CQAtomization arm of the experiment
            # (generation.atomic: false): the agent is never called, and
            # the totality pass below turns every competency question
            # into its own single atomic question, so the later stages
            # see the original phrasing unchanged
            print("Step 1: CQAtomization disabled (generation.atomic: "
                  "false); every competency question passes through "
                  "unsplit...")
        else:
            print("Step 1: Atomizing competency questions, one per call...")
            for i, c in enumerate(self.cqs, start=1):
                print(f"[CQAtomization] question {i}/{len(self.cqs)}: "
                      f"{c['id']}")
                # every question gets its parse_retries attempts; whatever
                # goes wrong - an invalid reply, a wrong id, or a provider
                # error - this question falls back to itself as its own
                # atomic question and the loop moves on, never ending early
                entry = None
                try:
                    reply = self.run_validated(
                        "cq_atomization", lambda r: r.atomization,
                        ids={c["id"]},
                        competency_question=f"{c['id']}: {c['value']}")
                    self.log_agent_step("cq_atomization", reply)
                    entry = reply.for_cq(c["id"])
                except Exception as e:
                    print(f"Warning: {e}; keeping {c['id']} unsplit")
                    self.log_agent_step("cq_atomization", None,
                                        error_message=str(e))
                if entry is None:
                    entry = CQAtomization(cq_id=c["id"])
                atomization.atomization.append(entry)
        # the pipeline is total: a question the agent skipped, and a
        # question whose entry carries no atomic questions, passes
        # through unchanged as its own atomic question (template "none")
        for c in self.cqs:
            entry = atomization.for_cq(c["id"])
            if entry is None:
                entry = CQAtomization(cq_id=c["id"])
                atomization.atomization.append(entry)
            if not entry.atomic_cqs:
                entry.atomic_cqs.append(AtomicCQ(
                    atomic_cq_id=f"{c['id']}_a1", atomic_cq=c["value"],
                    template="none"))
                entry.recomposition = entry.recomposition or "identity"
        # every atomic question is renumbered onto the canonical
        # "<cq_id>_a<n>" form, in answering order, so the atomic ids always
        # carry the real question id as their prefix even when the model
        # wrote the question id differently; the [#id] references inside the
        # question texts and the recomposition are rewritten with them so
        # nothing points at an id that no longer exists
        for entry in atomization.atomization:
            renamed = {}
            for i, a in enumerate(entry.atomic_cqs, start=1):
                new_id = f"{entry.cq_id}_a{i}"
                if a.atomic_cq_id and a.atomic_cq_id != new_id:
                    renamed[a.atomic_cq_id] = new_id
                a.atomic_cq_id = new_id
            if renamed:
                # one simultaneous pass, longest id first, so a rename that
                # swaps two ids cannot be applied twice
                pattern = re.compile("|".join(
                    re.escape(old) for old in
                    sorted(renamed, key=len, reverse=True)))
                for a in entry.atomic_cqs:
                    a.atomic_cq = pattern.sub(
                        lambda m: renamed[m.group(0)], a.atomic_cq)
                entry.recomposition = pattern.sub(
                    lambda m: renamed[m.group(0)], entry.recomposition)
            if not entry.recomposition:
                entry.recomposition = ("identity"
                                       if len(entry.atomic_cqs) == 1
                                       else "AND of all atomic answers")
        # the atomic ids and texts join the id -> text table the OWL
        # serializer resolves dc:source contents against, and every
        # atomic id remembers its original question, so a source naming
        # an atomic question can also name the question it came from
        for entry in atomization.atomization:
            for a in entry.atomic_cqs:
                self.atomic_parent[a.atomic_cq_id] = entry.cq_id
                self.cq_texts.setdefault(a.atomic_cq_id, a.atomic_cq)
        return {"atomization": atomization}

    def extract_terms(self, state: State) -> State:
        print("Step 2: Extracting terms from atomic competency questions...")
        atomization = state["atomization"]
        atomic_terms = self.run_validated(
            "term_extraction", lambda r: r.terms,
            ids=atomization.atomic_ids(),
            id_field="atomic_cq_id",
            atomic_cqs=json.dumps(
                atomization_document(self.cqs, atomization),
                indent=2, ensure_ascii=False))
        self.log_agent_step("term_extraction", atomic_terms)
        mapping = build_term_mapping(self.cqs, atomization, atomic_terms)
        self.save_json(self.cq_terms_path, mapping)
        # the raw mapping doubles as the term map of record, so the later
        # stages still have one when TermIdentification and TermRefinement
        # are toggled off
        term_map = TermMap.model_validate({"terms": mapping})
        return {"atomic_terms": atomic_terms, "term_map": term_map}

    def identify_terms(self, state: State) -> State:
        print(f"Step {self.identify_step}: Identifying terms "
              "(per-question filtering)...")
        mapping = build_term_mapping(self.cqs, state["atomization"],
                                     state["atomic_terms"])
        identified = repair_mapping_ids(
            self.run_validated(
                "term_identification", lambda r: r.terms,
                term_mapping=json.dumps(mapping, indent=2,
                                        ensure_ascii=False)),
            state["atomization"])
        self.log_agent_step("term_identification", identified)
        return {"identified": identified, "term_map": identified}

    def refine_terms(self, state: State) -> State:
        print(f"Step {self.refine_step}: Refining terms (batch level)...")
        # the input is whatever the previous stage produced: the
        # TermIdentification output, or the raw TermExtraction mapping
        # when identification is toggled off
        document = build_final_document(self.cqs, state["atomization"],
                                        state["term_map"])
        refined = repair_mapping_ids(
            self.run_validated(
                "term_refinement", lambda r: r.terms,
                term_mapping=json.dumps(document, indent=2,
                                        ensure_ascii=False)),
            state["atomization"])
        # the deterministic specification prune belongs to this stage, so
        # it runs before the entry is written and its removals are part
        # of that entry: one successful agent call is ONE step in
        # steps.json, whose output is the stage's final term map plus
        # the classes the prune removed from it
        dropped = prune_specification_classes(refined, state["atomization"])
        if dropped:
            print("[TermRefinement] specification prune removed: "
                  + ", ".join(dropped))
        output = refined.model_dump()
        if dropped:
            output["removed_classes"] = dropped
        agent = self.agents["term_refinement"]
        self.log_step(agent=agent.name, input_text=agent.last_prompt,
                      output=output,
                      extra=({"thinking": agent.last_thinking}
                             if agent.last_thinking else None))
        return {"term_map": refined}

    def finalize_terms(self, state: State) -> State:
        # the final JSON object of the requirements phase, built from
        # whichever term stage ran last (extraction, identification or
        # refinement) and handed to the generation stages
        final = build_final_document(self.cqs, state["atomization"],
                                     state["term_map"])
        self.save_json(self.cq_refined_terms_path, final)
        print(f"Requirements phase complete: {self.cq_refined_terms_path}")
        return {"final_document": final,
                "terms": state["term_map"].to_terms()}

    def expand_atomic_sources(self, answer: Answer) -> None:
        """Every competency_question source naming an atomic question is
        rewritten onto the canonical id spelling and joined by a second
        entry naming the original question it came from, so the entity's
        dc:source points to the atomic question and the original
        question at the same time."""
        table = canonical_ids(list(self.atomic_parent)
                              + [c["id"] for c in self.cqs])
        for entity in answer.OWL:
            seen = {source_key(s) for s in entity.Source}
            expanded = []
            for s in entity.Source:
                expanded.append(s)
                if s.sourcetype != "competency_question":
                    continue
                content = s.content.split(":")[0].strip()
                real = (content if content in self.cq_texts
                        else table.get(normalize_id(content), ""))
                if not real:
                    continue
                s.content = real
                parent = self.atomic_parent.get(real)
                if not parent:
                    continue
                extra = SourceEntry(sourcetype="competency_question",
                                    content=parent)
                if source_key(extra) not in seen:
                    seen.add(source_key(extra))
                    expanded.append(extra)
            entity.Source = expanded

    def generate_axioms(self, state: State) -> State:
        print(f"Step {self.axiom_step}: Generating atomic axioms...")
        axioms = self.run_validated(
            "axiom_generation", lambda r: r.axioms,
            competency_questions=format_cqs(self.cqs),
            term_map=json.dumps(state["final_document"], indent=2,
                                ensure_ascii=False))
        self.log_agent_step("axiom_generation", axioms)
        document = []
        for c in self.cqs:
            a = axioms.for_cq(c["id"])
            document.append({"cq_id": c["id"], "cq": c["value"],
                             "axioms": a.axioms if a else []})
        self.save_json(self.axiom_path, document)
        return {"axioms": axioms}

    def generate_tests(self, state: State) -> State:
        print(f"Step {self.test_step}: Generating Themis tests...")
        tests = self.run_validated(
            "test_generation", lambda r: r.tests,
            competency_questions=format_cqs(self.cqs),
            term_map=json.dumps(state["final_document"], indent=2,
                                ensure_ascii=False))
        self.log_agent_step("test_generation", tests)
        document = []
        for c in self.cqs:
            t = tests.for_cq(c["id"])
            document.append({"cq_id": c["id"], "cq": c["value"],
                             "tests": t.tests if t else []})
        self.save_json(self.tests_path, document)
        return {"tests": tests}

    def generate_ontology(self, state: State) -> State:
        print(f"Step {self.onto_step}: Generating initial ontology "
              f"(inputs: {', '.join(self.config.generation_inputs) or 'none'})...")
        try:
            answer = self.agents["onto_generation"].run(
                competency_questions=self.cq_input(state),
                context=self.build_context(state))
        finally:
            self.log_agent_failures("onto_generation")
        answer = stamp_answer(answer, "OntoGeneration Agent")
        self.expand_atomic_sources(answer)
        self.log_agent_step("onto_generation", answer)
        self.write_ontology(answer)
        self.write_ontology(answer, self.onto_initial_path)
        print(f"Initial ontology snapshot: {self.onto_initial_path}")
        # the provenance ontology exists from this moment on
        self.write_provenance(answer)
        print(f"Step {self.onto_step + 1}: Validating with tools and the "
              "OntoCorrection Agent...")
        return {"answer": answer, "tool_index": 0, "attempt": 1,
                "round": 1, "round_failed": False, "phase": "loop"}

    async def check_tool(self, state: State) -> State:
        tool, _ = self.tools[state["tool_index"]]
        if state.get("phase") == "verify":
            where = (f"round {state['round']}/{self.config.loop_rounds} "
                     "verification")
            print(f"[{where}] [{tool}] check")
        else:
            where = (f"round {state['round']}/{self.config.loop_rounds}, "
                     f"check {state['attempt']}/{self.config.tool_retries}")
            print(f"[round {state['round']}/{self.config.loop_rounds}] "
                  f"[{tool}] check {state['attempt']}/{self.config.tool_retries}")
        args = {"ontology_path": self.onto_path}
        if tool == "hermit_consistency":
            args["jar_path"] = str(self.config.hermit_jar)
        if tool == "cq_coverage":
            args["cqs"] = [
                {"id": c["id"], "value": c["value"],
                 "terms": (t.model_dump(exclude={"cq_id"})
                           if (t := state["terms"].for_cq(c["id"])) else {})}
                for c in self.cqs]
        if tool == "themis_test":
            args["cqs"] = [
                {"id": c["id"], "value": c["value"],
                 "tests": (t.tests if (t := state["tests"].for_cq(c["id"])) else [])}
                for c in self.cqs]
            args["mode"] = self.config.themis_mode
            args["jar_path"] = str(self.config.themis_jar)
            args["endpoint"] = self.config.themis_endpoint
        try:
            result = await self.toolbox.call(tool, **args)
        except Exception as e:
            # the tool call itself crashed (transport or server error):
            # recorded as a failed step before the error propagates
            self.log_step(tool=tool,
                          error_message=f"tool call failed: {e}",
                          input_text=self.serialize(state["answer"]),
                          output="",
                          extra={"round": state.get("round"),
                                 "phase": state.get("phase"),
                                 "attempt": state.get("attempt")})
            raise
        # the FULL raw tool return value is saved in steps.json on every
        # call, passed or failed; error_message keeps the tool's report
        # on failure, falling back to whatever error text the result
        # carries when a report is missing
        error_message = ""
        if not result.get("passed"):
            error_message = str(
                result.get("report") or result.get("error")
                or result.get("errors") or result.get("raw")
                or "tool reported failure without a report")
        self.log_step(
            tool=tool,
            error_message=error_message,
            input_text=self.serialize(state["answer"]),
            output=result,
            extra={"round": state.get("round"),
                   "phase": state.get("phase"),
                   "attempt": state.get("attempt")})
        status = "passed" if result.get("passed") else "FAILED"
        if result.get("tool_error"):
            status += " (tool error - no correction attempted)"
        show(f"{tool} | result: {status} ({where})",
             json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("passed"):
            return {"result": result}
        # A check that did not pass marks this phase as "not clean" even if a
        # correction later fixes it: a round that found something has to be
        # verified afterwards, a round that found nothing has nothing to
        # verify. The flag is reset when a phase or a round begins.
        return {"result": result, "round_failed": True}

    def correct_ontology(self, state: State) -> State:
        tool, kind = self.tools[state["tool_index"]]
        report = state["result"].get("report", "")
        try:
            corrected = self.agents["onto_correction"].run(
                competency_questions=self.cq_input(state),
                context=self.build_context(state),
                ontology=self.serialize(state["answer"]),
                kind=kind,
                report=report)
        except RuntimeError as e:
            print(f"Warning: {e}; keeping previous ontology")
            self.log_agent_step("onto_correction", None, tool=tool,
                                error_message=str(e))
            return {"attempt": state["attempt"] + 1}
        finally:
            self.log_agent_failures("onto_correction", tool=tool)
        loop_info = (f"loop_rounds: {state['round']} "
                     f"tool_retries: {state['attempt']}")
        corrected = stamp_answer(corrected, "OntoCorrection Agent", tool=tool,
                                 event=loop_info)
        self.expand_atomic_sources(corrected)
        self.log_agent_step("onto_correction", corrected, tool=tool,
                            error_message=str(report),
                            extra={"round": state.get("round"),
                                   "attempt": state.get("attempt")})
        answer = merge_answers(state["answer"], corrected)
        self.write_ontology(answer)
        self.write_provenance(answer)
        return {"answer": answer, "attempt": state["attempt"] + 1}

    def advance(self, state: State) -> State:
        # move to the next tool, into verification, the next round, or finish.
        # round_failed was set by check_tool the moment any check of this
        # phase did not pass
        failed = bool(state.get("round_failed"))
        next_index = state["tool_index"] + 1
        if next_index < len(self.tools):
            return {"tool_index": next_index, "attempt": 1}
        if state.get("phase") == "loop":
            if not failed:
                # every tool passed on the first pass, so no correction ever
                # touched the ontology: the document the tools just checked
                # IS the verified one and the loop stops here - there is
                # nothing a verification pass could add
                print(f"\nRound {state['round']} passed with no findings: "
                      "every tool passes, no verification pass needed")
                return {"done": True, "all_passed": True}
            # something was found (and corrected) during the round: verify
            # every tool against the corrected ontology to see whether all
            # of it is really solved
            print(f"\nRound {state['round']} finished with findings: "
                  "verifying every tool against the corrected ontology")
            return {"phase": "verify", "tool_index": 0, "attempt": 1,
                    "round_failed": False}
        if not failed:
            print("\nVerification passed: every tool passes on the "
                  "latest ontology")
            return {"done": True, "all_passed": True}
        if state["round"] < self.config.loop_rounds:
            print("\nVerification FAILED: starting the next round")
            return {"phase": "loop", "round": state["round"] + 1,
                    "tool_index": 0, "attempt": 1, "round_failed": False}
        print("\nVerification FAILED and no rounds left")
        return {"done": True, "all_passed": False}

    def route_after_check(self, state: State) -> str:
        if state.get("phase") == "verify":
            return "advance"
        result = state["result"]
        if result.get("passed") or result.get("tool_error"):
            return "advance"
        if state["attempt"] <= self.config.tool_retries:
            return "correct"
        return "advance"

    def route_after_advance(self, state: State) -> str:
        return "finish" if state.get("done") else "check"

    def build_graph(self):
        graph_builder = StateGraph(State)
        # atomize_cqs and extract_terms always run as nodes (with
        # generation.atomic: false the atomize node calls no agent);
        # TermIdentification, TermRefinement, AxiomGeneration and
        # TestGeneration are added only when their toggles enable them,
        # and finalize_terms builds the final term document from
        # whichever term stage ran last
        graph_builder.add_node("atomize_cqs", self.atomize_cqs)
        graph_builder.add_node("extract_terms", self.extract_terms)
        graph_builder.add_node("finalize_terms", self.finalize_terms)
        graph_builder.add_node("generate_ontology", self.generate_ontology)
        graph_builder.add_node("check_tool", self.check_tool)
        graph_builder.add_node("correct_ontology", self.correct_ontology)
        graph_builder.add_node("advance", self.advance)

        graph_builder.add_edge(START, "atomize_cqs")
        graph_builder.add_edge("atomize_cqs", "extract_terms")
        previous = "extract_terms"
        if self.config.run_identification:
            graph_builder.add_node("identify_terms", self.identify_terms)
            graph_builder.add_edge(previous, "identify_terms")
            previous = "identify_terms"
        if self.config.run_refinement:
            graph_builder.add_node("refine_terms", self.refine_terms)
            graph_builder.add_edge(previous, "refine_terms")
            previous = "refine_terms"
        graph_builder.add_edge(previous, "finalize_terms")
        previous = "finalize_terms"
        if self.config.run_axioms:
            graph_builder.add_node("generate_axioms", self.generate_axioms)
            graph_builder.add_edge(previous, "generate_axioms")
            previous = "generate_axioms"
        if self.config.run_tests:
            graph_builder.add_node("generate_tests", self.generate_tests)
            graph_builder.add_edge(previous, "generate_tests")
            previous = "generate_tests"
        graph_builder.add_edge(previous, "generate_ontology")
        graph_builder.add_edge("generate_ontology", "check_tool")
        graph_builder.add_conditional_edges(
            "check_tool", self.route_after_check,
            {"correct": "correct_ontology", "advance": "advance"})
        graph_builder.add_edge("correct_ontology", "check_tool")
        graph_builder.add_conditional_edges(
            "advance", self.route_after_advance,
            {"check": "check_tool", "finish": END})
        return graph_builder.compile()


async def run_pipeline(pipeline: MASEOPipeline) -> bool:
    async with MCPToolbox() as toolbox:
        pipeline.toolbox = toolbox
        graph = pipeline.build_graph()
        final = await graph.ainvoke({}, {"recursion_limit": 1000})
    pipeline.write_final(final["answer"])
    return bool(final.get("all_passed"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MASEO atomic LangGraph pipeline: atomize competency "
                    "questions, extract, identify and refine terms, then "
                    "generate a validated OWL ontology. "
                    "Example: python agent_graph.py vgo")
    parser.add_argument("domain")
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    args = parser.parse_args()

    config = load_config(args.config, args.domain)
    pipeline = MASEOPipeline(config)
    print(f"MASEO atomic starting for domain '{config.domain}'")
    print(f"Model: {config.model_provider}/{config.model_id}")
    print(f"Base URI: {config.base_uri}")
    print(f"CQ file: {config.cq_file}")
    stages = ["CQAtomization"] if config.run_atomization else []
    stages.append("TermExtraction")
    if config.run_identification:
        stages.append("TermIdentification")
    if config.run_refinement:
        stages.append("TermRefinement")
    if config.run_axioms:
        stages.append("AxiomGeneration")
    if config.run_tests:
        stages.append("TestGeneration")
    stages += ["OntoGeneration", "validation loop"]
    print("Pipeline: " + " > ".join(stages))
    print(f"Experiment mode: {config.mode}")
    print(f"Output dir: {config.output_dir}")
    print(f"OntoGeneration context: {', '.join(config.generation_inputs)}")
    print(f"Tools: {', '.join(t[0] for t in pipeline.tools)}")
    print(f"Tool retries: {config.tool_retries}  "
          f"Loop rounds: {config.loop_rounds}")

    passed = asyncio.run(run_pipeline(pipeline))
    print("\n" + "=" * 50)
    print("ALL CHECKS PASSED" if passed else "NOT ALL CHECKS PASSED")
    print("=" * 50)
    print(f"Initial:       {pipeline.onto_initial_path}")
    print(f"Final:         {pipeline.onto_path}")
    print(f"Provenance:    {pipeline.onto_prov_path}")
    print(f"Raw terms:     {pipeline.cq_terms_path}")
    print(f"Refined terms: {pipeline.cq_refined_terms_path}")
    if config.run_axioms:
        print(f"Axioms:        {pipeline.axiom_path}")
    if config.run_tests:
        print(f"Tests:         {pipeline.tests_path}")
    print(f"Steps:         {pipeline.steps_path}")


if __name__ == "__main__":
    main()
