import argparse
import copy
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
 
import yaml
 
DATASET_DIR = Path("./dataset").resolve()
CQS_DIR = DATASET_DIR / "cqs"
METHOD_DIRS = {"agent": DATASET_DIR / "agent", "normal": DATASET_DIR / "normal"}
METHOD_FLAG = {"agent": "true", "normal": "false"}
TEMP_CONFIG = DATASET_DIR / ".run_config.yaml"
 
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
 

def safe_name(s: str) -> str:
    return _LABEL_SAFE.sub("_", s).strip("_") or "unnamed"
 

    
def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch runner for MASEO: models × CQ files × method."
    )
    parser.add_argument("--batch", default="./batch.yaml",
                        help="Path to the batch manifest YAML.")
    parser.add_argument("--cli", default="./cli.py",
                        help="Path to MASEO's cli.py.")
    parser.add_argument("--config", default="./config.yaml",
                        help="Local config.yaml used as a template for the temp config.")
    parser.add_argument("--force", action="store_true",
                        help="Re-run combinations even if outputs already exist.")
    parser.add_argument("--dry_run", action="store_true",
                        help="List planned runs, do not execute them.")
    parser.add_argument("--keep_temp", action="store_true",
                        help="Keep ./dataset/.run_config.yaml after the run finishes.")
    return parser
 

    
def load_models(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Batch manifest not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f) or {}
    models = manifest.get("models", [])
    if not models:
        raise ValueError(f"Manifest {path} has no `models:` list.")
    for m in models:
        if "provider" not in m or "id" not in m:
            raise ValueError(f"Each model entry needs `provider` and `id`. Got: {m}")
    return models
 

    
def absolutize_paths(cfg: Dict[str, Any], base_dir: Path) -> None:
    def _abs(p: str) -> str:
        path = Path(p).expanduser()
        return str(path) if path.is_absolute() else str((base_dir / path).resolve())
 
    if "hermit" in cfg and "jar_path" in cfg["hermit"]:
        cfg["hermit"]["jar_path"] = _abs(cfg["hermit"]["jar_path"])
    if "oops" in cfg and "request_template" in cfg["oops"]:
        cfg["oops"]["request_template"] = _abs(cfg["oops"]["request_template"])
 
 
    
    
def write_temp_config(base_config: Dict[str, Any], model_entry: Dict[str, Any],
                      base_config_dir: Path) -> Path:
    cfg = copy.deepcopy(base_config)
    absolutize_paths(cfg, base_config_dir)
 
    provider = model_entry["provider"].lower().strip()
    cfg.setdefault("model", {})["provider"] = provider
    cfg["model"].setdefault(provider, {})
    cfg["model"][provider]["id"] = model_entry["id"]
 
    # Optional per-model overrides
    for shared_key in ("max_tokens", "temperature"):
        if shared_key in model_entry:
            cfg["model"][shared_key] = model_entry[shared_key]
    if model_entry.get("api_key"):
        cfg["model"][provider]["api_key"] = model_entry["api_key"]
    if provider == "ollama" and model_entry.get("host"):
        cfg["model"][provider]["host"] = model_entry["host"]
    if model_entry.get("base_url"):
        cfg["model"][provider]["base_url"] = model_entry["base_url"]
 
    TEMP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(TEMP_CONFIG, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    return TEMP_CONFIG
 

    
def run_one(cli_path: Path, config_path: Path, cqs_path: Path,
            save_path: Path, log_path: Path, agent_method_flag: str) -> bool:
    cmd = [
        sys.executable, str(cli_path),
        "--config", str(config_path),
        "--cqs_file", str(cqs_path),
        "--save_file", str(save_path),
        "--agent_method", agent_method_flag,
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.parent.mkdir(parents=True, exist_ok=True)
 
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"$ {' '.join(cmd)}\n\n")
        logf.flush()
        result = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT, text=True)
 
    return result.returncode == 0 and save_path.exists() and save_path.stat().st_size > 0
 

    
def main() -> None:
    args = get_parser().parse_args()
 
    batch_path = Path(args.batch).resolve()
    cli_path = Path(args.cli).resolve()
    base_config_path = Path(args.config).resolve()
 
    if not cli_path.exists():
        raise FileNotFoundError(f"cli.py not found: {cli_path}")
    if not base_config_path.exists():
        raise FileNotFoundError(f"config.yaml not found: {base_config_path}")
    if not CQS_DIR.exists():
        raise FileNotFoundError(
            f"CQS directory not found: {CQS_DIR}. "
            f"Put your CQ JSON files in ./dataset/cqs/."
        )
 
    models = load_models(batch_path)
    cqs_files = sorted(CQS_DIR.glob("*.json"))
    if not cqs_files:
        raise FileNotFoundError(f"No *.json files in {CQS_DIR}.")
 
    with open(base_config_path, "r", encoding="utf-8") as f:
        base_config = yaml.safe_load(f)
 
    # Plan
    plan: List[Dict[str, Any]] = []
    for model_entry in models:
        model_dir_name = safe_name(model_entry["id"])
        for cqs_path in cqs_files:
            cqs_stem = cqs_path.stem
            if cqs_stem.endswith("_cqs"):
                cqs_stem = cqs_stem[:-4]
            ontology_label = safe_name(cqs_stem)
 
            for method in ("agent", "normal"):
                method_root = METHOD_DIRS[method] / model_dir_name
                ontology_path = method_root / "ontology" / f"{ontology_label}_{method}_ontology.owl"
                log_path = method_root / "log" / f"{ontology_label}_{method}_log.txt"
                plan.append({
                    "model_entry": model_entry,
                    "model_dir_name": model_dir_name,
                    "cqs_path": cqs_path,
                    "ontology_label": ontology_label,
                    "method": method,
                    "ontology_path": ontology_path,
                    "log_path": log_path,
                })
 
    print(f"Planned runs: {len(plan)}  ({len(models)} models × {len(cqs_files)} cqs × 2 methods)")
 
    if args.dry_run:
        for p in plan:
            print(f"  [{p['method']:6s}] {p['model_dir_name']:35s}  "
                  f"{p['ontology_label']:20s}  -> {p['ontology_path']}")
        return
 
    skipped = succeeded = failed = 0
 
    try:
        for i, p in enumerate(plan, start=1):
            method = p["method"]
            ontology_path: Path = p["ontology_path"]
            log_path: Path = p["log_path"]
 
            print(f"\n[{i}/{len(plan)}] {p['model_dir_name']} | {p['ontology_label']} | {method}")
 
            if not args.force \
               and ontology_path.exists() and ontology_path.stat().st_size > 0 \
               and log_path.exists() and log_path.stat().st_size > 0:
                print("  Skipping — already done.")
                skipped += 1
                continue
 
            # Refresh the temp config for this model. Cheap — just one file.
            temp_cfg_path = write_temp_config(
                base_config, p["model_entry"], base_config_path.parent
            )
 
            success = run_one(
                cli_path=cli_path,
                config_path=temp_cfg_path,
                cqs_path=p["cqs_path"],
                save_path=ontology_path,
                log_path=log_path,
                agent_method_flag=METHOD_FLAG[method],
            )
            if success:
                succeeded += 1
                print(f"  OK  -> {ontology_path}")
            else:
                failed += 1
                print(f"  FAIL — see {log_path}")
 
    finally:
        if not args.keep_temp and TEMP_CONFIG.exists():
            try:
                TEMP_CONFIG.unlink()
            except OSError:
                pass
 
    print(f"\nDone. {succeeded} ok, {failed} failed, {skipped} skipped.")
 
 
if __name__ == "__main__":
    main()