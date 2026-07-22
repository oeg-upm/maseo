---
name: maseo-agents
description: Output contract for the MASEO GenerationAgent and CorrectionAgent.
  The body of this file (everything below the frontmatter) is appended to both
  agents' system prompts at runtime, with {base_uri} substituted. Edit THIS
  file to change WHAT the agents must produce; edit config.yaml (agents.*) to
  change WHO each agent is. Repo documentation lives in README.md.
---

# Output contract

## Deliverable

Return ONLY one complete OWL ontology as an RDF/XML document — no markdown
fences, no commentary, nothing before `<?xml` or after `</rdf:RDF>`. The
document must be well-formed and loadable.

## URI rule

The base URI `{base_uri}` already ends with `#`. Append local names DIRECTLY
to it (e.g. `rdf:about="{base_uri}Wine"`); never write `#LocalName`,
`{base_uri}#LocalName`, or template text.

## Term rule

The competency questions come with a CQ&#8594;terms mapping (produced by the
extraction agent). Every named term — classes, object/data properties,
individuals — MUST exist in the ontology under its mapped name (the
`cq_coverage` check matches entity local names and rdfs:labels,
case/space-insensitive). Axioms in the mapping guide the modelling but are
not string-matched. When a report lists MISSING TERMS, add each one under
exactly that name.

## Test rule (Themis)

The mapping also lists `tests` — Themis verification tests per CQ (e.g.
`Wine SubClassOf producedIn some Region`), written in the official Themis
test catalogue (https://themis.linkeddata.es/tests-info.html). The full
template set:

- existence: `ClassA type Class`, `propP type Property`,
  `IndividualI type ClassA`
- subsumption: `A SubClassOf B`; multiple inheritance / intersection
  `A SubClassOf B and C`; subsumption + relation
  `A SubClassOf B that p some C`; disjoint set
  `A SubClassOf M and S SubClassOf M that disjointWith A`
- class relations: `A disjointWith B`, `A equivalentTo B`
- object properties: `p domain A`, `p range B`, plain relation `A p B`,
  symmetry `p characteristic symmetricProperty`
- restrictions: universal `A SubClassOf p only B`;
  union range `A SubClassOf p only B or C`; cardinality
  `A SubClassOf p min|max|exactly N B`; compound
  `A SubClassOf p min N B and B SubClassOf q some C`
- data properties: `A p <datatype>` (never `domain`/`range` tests)
- NOTE: existential tests (`A SubClassOf p some B`) are NOT used - Themis
  executes them with a ¬B probe individual that a declared `rdfs:range B`
  makes inconsistent, so they return Conflict forever. The plain relation
  `A p B` is the executable equivalent; when such a test fails, model the
  relation (property + domain/range + the restriction if the CQ needs it).

They are the GOLD STANDARD of the run: fixed at extraction time and never
regenerated. The suite is persisted as `<domain>_testsuite.ttl` following
the Verification Test Case ontology (VTC, https://w3id.org/def/vtc#): each
CQ is a `vtc:Requirement` (`vtc:requirementId`, `dcterms:description`) and
each test a `vtc:TestCaseDesign` whose `vtc:desiredBehaviour` holds the
expression, linked via `vtc:comesFromRequirement` and grouped in a
`vtc:TestSuite`. The `themis_test` check
executes them against the ontology; a CQ counts as covered only when ALL its
tests return Passed. Model the ontology so each test holds, using EXACTLY
the term names in the test (Themis matches case-sensitively). A test
`Class SubClassOf prop some <datatype>` needs BOTH the domain/restriction
side and the property declared with that exact datatype range. When a report
lists FAILING TESTS: Undefined = the term is missing, add it under that
exact name; Incorrect = the term exists with the wrong type, redeclare it;
Absent = the tested knowledge is not modelled, add the subclass/domain/
range/restriction axiom the test states; Conflict = the ontology contradicts
the test, correct the conflicting axioms. Never rename or delete other
entities while fixing a test — fixes must be additive and local.

## Provenance rule

On every entity add `rdfs:label`, `rdfs:comment`, and a `dc:source`
(namespace http://purl.org/dc/elements/1.1/) with one tagged line per source:

    (competency_question) CQ6   <- each CQ the entity serves (required for coverage checking)
    (pitfall) P11               <- OOPS! pitfall that motivated a change
    (error_message) <text>      <- HermiT/syntax error that motivated a change

## Individuals rule

Only introduce individuals (owl:NamedIndividual / instances) when a competency
question requires instance-level data; otherwise model at the class level. For
EACH individual, assert everything needed to answer its competency questions:
its class type(s) via rdf:type, its object-property relations to other
individuals, and its data-property values.

## Correction rules (when repairing from a diagnostic report)

1. Return the FULL corrected document, not a fragment.
2. Preserve every entity and its provenance (rdfs:label, rdfs:comment,
   dc:source, vaem:rationale). Never delete provenance.
3. Append a short vaem:rationale to each entity you fix.
4. For every UNCOVERED competency question, add/extend the needed
   classes/properties/axioms and record its id in that entity's dc:source as
   `(competency_question) <id>`.
5. Declare `rdfs:domain`/`rdfs:range` DIRECTLY on the property element.
   NEVER use `owl:Axiom` annotated-axiom reification (owl:annotatedSource/
   Property/Target) — Themis's executor mishandles annotated axioms and the
   affected terms start failing as Incorrect.
6. A term is EITHER a class OR an individual — never declare the same local
   name as both (no punning): Themis types each term once, and a punned
   term makes its tests fail as Incorrect.

## Acceptance criteria

The ontology is finished only when all five checks pass:
`syntax_check` errors == [] (one well-formed RDF/XML document, rdf:RDF root,
nothing before `<?xml` or after `</rdf:RDF>`); `oops_scan` major_count == 0;
`hermit_consistency` consistent with no unsatisfiable classes;
`cq_coverage` uncovered == []; `themis_test` uncovered == [].
