import xml.etree.ElementTree as ET
 
from agno.workflow import Workflow
 
from config import Config
from utils import is_owl, merge_rationale, parse_answer
from oops_validation import OOPSValidation, OOPSUnreadableError, format_oops_nl
from reasoner import reason_ontology
 
from agents import (
    ontology_generation_agent,
    syntax_repair_agent,
    logical_consistency_agent,
    pitfall_resolution_agent,
)
 

    
class MASEOWorkflow(Workflow):
    """MASEO workflow"""
 
    def __init__(self, competency_questions: str, config: Config):
        super().__init__(name="MASEO Workflow")
        self.cqs = competency_questions
        self.config = config
        self.base_uri = config.base_uri
        self.max_retries = config.default_retries
 
        self.ontology_gen_agent = ontology_generation_agent.create(config)
        self.ontology_syntax_agent = syntax_repair_agent.create(config)
        self.ontology_hermit_agent = logical_consistency_agent.create(config)
        self.ontology_oops_agent = pitfall_resolution_agent.create(config)

    def run(self, agent_method: bool) -> str:
        if agent_method:
            print("Executing MASEO workflow")
 
            # Step 1: Generate + syntax
            print("Step 1: Generating initial ontology draft...")
            ontology = self._generate_initial_ontology()
            print(f"Initial ontology draft:\n{ontology}")
            ontology = self._ensure_valid_syntax(ontology)
            print("Syntax validation passed")
 
            # Step 2: Hermit consistency
            print("Step 2: Consistency checking with Hermit...")
            ontology = self._consistency_check_loop(ontology)
            ontology = self._ensure_valid_syntax(ontology)
            print("Consistency checking passed")
 
            # Step 3: OOPS pitfall resolution
            # OOPSUnreadableError propagates up to cli.py unchanged
            print("Step 3: Pitfall resolution with OOPS...")
            ontology = self._pitfall_resolution_loop(ontology)
            ontology = self._ensure_valid_syntax(ontology)
            print("Pitfall resolution completed")
 
        else:
            print("Executing normal workflow")
            ontology = self._generate_initial_ontology()
            ontology = self._ensure_valid_syntax(ontology)
            print("Syntax validation passed")
 
        return ontology
 
    def _generate_initial_ontology(self) -> str:
        gen_prompt = self.config.render_prompt(
            "ontology_generation",
            competency_questions=self.cqs,
        )
        print(gen_prompt)
        gen_response = self.ontology_gen_agent.run(gen_prompt)
        print(gen_response)
        return parse_answer(
            gen_response, agent_name="Ontology Generation Agent"
        ).to_owl_document(self.base_uri)
 
    def _ensure_valid_syntax(self, ontology: str) -> str:
        attempts = 0
        while not is_owl(ontology):
            if attempts >= self.max_retries:
                print(
                    f"Warning: syntax still invalid after {self.max_retries} "
                    "retries; returning current ontology."
                )
                break
            ontology = self._syntax_validation_step(ontology)
            attempts += 1
        return ontology
 
    def _syntax_validation_step(self, ontology: str) -> str:
        try:
            ET.fromstring(ontology)
            return ontology
        except ET.ParseError as e:
            print(f"Syntax error detected: {e}")
            syntax_prompt = self.config.render_prompt(
                "syntax_repair",
                ontology=ontology,
                error=str(e),
            )
            response = self.ontology_syntax_agent.run(syntax_prompt)
            new_answer = parse_answer(response, agent_name="Syntax Repair Agent")
            new_answer = merge_rationale(ontology, new_answer)
            return new_answer.to_owl_document(self.base_uri)
 
    def _consistency_check_loop(self, ontology: str) -> str:
        stdout, stderr = reason_ontology(
            ontology,
            hermit_jar=self.config.hermit_jar,
        )
        if stdout:
            print(f"Hermit stdout:\n{stdout}")
        if stderr:
            print(f"Hermit stderr:\n{stderr}")
            hermit_prompt = self.config.render_prompt(
                "logical_consistency",
                ontology=ontology,
                hermit_report=stderr,
            )
            response = self.ontology_hermit_agent.run(hermit_prompt)
            new_answer = parse_answer(response, agent_name="Logical Consistency Agent")
            new_answer = merge_rationale(ontology, new_answer)
            ontology = new_answer.to_owl_document(self.base_uri)
        return ontology
 
    def _pitfall_resolution_loop(self, ontology: str) -> str:
        oops = OOPSValidation(
            ontology,
            api_url=self.config.oops_api_url,
            request_template_path=self.config.oops_request_template,
        )
        oops_response = oops.validate()
        print(f"OOPS response:\n{oops_response}")
 
        oops_pitfalls = format_oops_nl(oops_response)
 
        print(f"OOPS pitfall summary:\n{oops_pitfalls}")
        oops_prompt = self.config.render_prompt(
            "pitfall_resolution",
            ontology=ontology,
            pitfalls=oops_pitfalls,
        )
        response = self.ontology_oops_agent.run(oops_prompt)
        new_answer = parse_answer(response, agent_name="Pitfall Resolution Agent")
        new_answer = merge_rationale(ontology, new_answer)
        return new_answer.to_owl_document(self.base_uri)
 