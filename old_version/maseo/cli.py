import argparse
import json
from pathlib import Path
 
from config import load_config
from utils import format_cqs_prompt, is_owl, str2bool
from workflow import MASEOWorkflow
from oops_validation import OOPSUnreadableError
from reasoner import reason_ontology
 
def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MASEO Framework"
    )
    parser.add_argument(
        "--config",
        default="./config.yaml",
        type=str,
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--cqs_file",
        required=True,
        type=str,
        help="Path to the JSON file of competency questions. "
             "Expected format: [{'id': 'CQ1', 'value': '...'}, ...]",
    )
    parser.add_argument(
        "--save_file",
        required=True,
        type=str,
        help="Where to write the final OWL ontology.",
    )
    parser.add_argument(
        "--agent_method",
        default="true",
        type=str,
        help="Whether to run the full MASEO multi-agent workflow (true) "
             "or only the single-pass generation + syntax repair (false).",
    )
    return parser
 
def main() -> None:
    args = get_parser().parse_args()
    config = load_config(args.config)
    agent_method = str2bool(args.agent_method)
 
    max_runs = config.default_retries
    print(f"MASEO starting.")
    print(f"Max attempts: {max_runs}")
 
    with open(args.cqs_file, "r", encoding="utf-8") as f:
        cqs = json.load(f)
    cqs_prompt = format_cqs_prompt(cqs)
 
    retries_left = max_runs
    ontology = None
 
    while retries_left > 0:
        attempt = max_runs - retries_left + 1
        print(f"\n--- Attempt {attempt} / {max_runs} (retries left: {retries_left}) ---")
        try:
            workflow = MASEOWorkflow(cqs_prompt, config)
            ontology = workflow.run(agent_method)
            break  # success
 
        except OOPSUnreadableError as e:
            retries_left -= 1
            print(
                f"OOPS could not read the ontology: {e}\n"
                f"Restarting full pipeline from CQs. Retries left: {retries_left}"
            )
 
        except Exception as e:
            retries_left -= 1
            print(
                f"Workflow error: {e}\n"
                f"Restarting full pipeline from CQs. Retries left: {retries_left}"
            )
 
    if ontology is None:
        raise RuntimeError(
            f"Failed to produce a valid ontology after {max_runs} attempts."
        )
 
    print("\n" + "=" * 50)
    print("WORKFLOW COMPLETED SUCCESSFULLY!")
    print("=" * 50)
    print(f"Is valid OWL: {is_owl(ontology)}")
 
 
    save_path = Path(args.save_file)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(ontology)
    print(f"Ontology written to: {save_path}")
 
 
if __name__ == "__main__":
    main()
 