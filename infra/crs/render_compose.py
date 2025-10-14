#!/usr/bin/env python3
"""
Render Docker Compose files per worker from configuration directory.

This script reads configuration files (config-resource.yaml, config-worker.yaml,
config-crs.yaml) from a directory and generates a compose-<worker>.yaml file
for each worker defined in the configuration.

Example usage:
  python render_compose.py --config-dir ./example_configs --output-dir ./output
"""

import argparse
import shutil
import yaml
from pathlib import Path
from jinja2 import Template
from typing import Dict, Any, List

SCRIPT_DIR = Path(__file__).parent

def load_config(config_dir: Path) -> Dict[str, Any]:
  """Load all configuration files from the config directory."""
  config = {}

  # Load resource configuration
  resource_config_path = config_dir / "config-resource.yaml"
  if not resource_config_path.exists():
    raise FileNotFoundError(f"Required file not found: {resource_config_path}")
  with open(resource_config_path) as f:
    config['resource'] = yaml.safe_load(f)

  # Load worker configuration (optional)
  worker_config_path = config_dir / "config-worker.yaml"
  if worker_config_path.exists():
    with open(worker_config_path) as f:
      config['worker'] = yaml.safe_load(f)
  else:
    config['worker'] = {}

  # Load CRS configuration (optional)
  crs_config_path = config_dir / "config-crs.yaml"
  if crs_config_path.exists():
    with open(crs_config_path) as f:
      config['crs'] = yaml.safe_load(f)
  else:
    config['crs'] = {}

  return config


def get_crs_for_worker(worker_name: str, resource_config: Dict[str, Any]) -> List[Dict[str, Any]]:
  """
  Extract CRS configurations for a specific worker.

  Returns a list of CRS configurations with resource constraints applied.
  """
  crs_list = []
  crs_configs = resource_config.get('crs', {})

  for crs_name, crs_config in crs_configs.items():
    # Check if this CRS should run on this worker
    crs_workers = crs_config.get('workers', [])
    if worker_name not in crs_workers:
      continue

    # Get resource configuration for this CRS on this worker
    resources = crs_config.get('resources', {})

    # Determine CPUs and memory for this worker
    if isinstance(resources, dict):
      # Check if resources are per-worker or global
      if worker_name in resources:
        # Per-worker resource specification
        worker_resources = resources[worker_name]
        cpus = worker_resources.get('cpus', '0-3')
        memory = worker_resources.get('memory', '4G')
      elif 'cpus' in resources:
        # Global resource specification (same for all workers)
        cpus = resources.get('cpus', '0-3')
        memory = resources.get('memory', '4G')
      else:
        # No resources specified, use defaults
        cpus = '0-3'
        memory = '4G'
    else:
      # No resources specified
      cpus = '0-3'
      memory = '4G'

    crs_list.append({
      'name': crs_name,
      'cpus': cpus,
      'memory_limit': str(memory),
      'suffix': 'runner'
    })

  return crs_list


def render_compose_for_worker(worker_name: str, crs_list: List[Dict[str, Any]],
                              template_path: Path, oss_fuzz_path: Path,
                              crs_path: Path, project: str,
                              fuzzer_command: List[str], config_dir: Path) -> str:
  """Render the compose template for a specific worker."""
  if not template_path.exists():
    raise FileNotFoundError(f"Template file not found: {template_path}")

  template_content = template_path.read_text()
  template = Template(template_content)

  # Resolve config paths
  config_resource_path = (config_dir / "config-resource.yaml").resolve()
  config_dir_resolved = config_dir.resolve()

  rendered = template.render(
    crs_runners=crs_list,
    worker_name=worker_name,
    oss_fuzz_path=str(oss_fuzz_path),
    crs_path=str(crs_path),
    project=project,
    fuzzer_command=fuzzer_command,
    config_resource_path=str(config_resource_path),
    config_dir=str(config_dir_resolved)
  )

  return rendered


def main():
  parser = argparse.ArgumentParser(
    description='Render Docker Compose files per worker from configuration directory'
  )
  parser.add_argument(
    '--env-file',
    type=str,
    help='Optional path to environment file to include in generated compose files'
  )
  parser.add_argument(
    '--output-dir',
    type=str,
    default='.',
    help='Directory to write compose-<worker>.yaml files (default: current directory)'
  )
  parser.add_argument(
    '--config-dir',
    type=str,
    required=True,
    help='Directory containing config-resource.yaml, config-worker.yaml, config-crs.yaml'
  )
  parser.add_argument(
    '--crs-path',
    type=str,
    required=True,
    help='Path to the CRS directory'
  )
  parser.add_argument(
    '--project',
    type=str,
    required=True,
    help='OSS-Fuzz project name'
  )
  parser.add_argument(
    'fuzzer_command',
    nargs=argparse.REMAINDER,
    help='Fuzzer command and arguments to execute in the container (provide after all other arguments)'
  )

  args = parser.parse_args()

  # Validate that fuzzer_command is provided
  if not args.fuzzer_command:
    parser.error('fuzzer_command is required')

  # Convert to Path objects
  config_dir = Path(args.config_dir)
  output_dir = Path(args.output_dir)
  template_path = SCRIPT_DIR / "compose.yaml.j2"
  crs_path = Path(args.crs_path).resolve()

  # Compute OSS_FUZZ_PATH as parent.parent.parent of this script
  oss_fuzz_path = Path(__file__).parent.parent.parent.resolve()

  # Handle optional env file
  env_file = Path(args.env_file).resolve() if args.env_file else None

  # Ensure output directory exists
  output_dir.mkdir(parents=True, exist_ok=True)

  # Copy env file to output directory as .env if provided
  if env_file:
    if not env_file.exists():
      print(f"Error: Environment file not found: {env_file}")
      return 1
    dest_env = output_dir / ".env"
    shutil.copy2(env_file, dest_env)
    print(f"Copied environment file to: {dest_env}")

  # Load configurations
  print(f"Loading configuration from: {config_dir}")
  try:
    config = load_config(config_dir)
  except FileNotFoundError as e:
    print(f"Error: {e}")
    return 1

  resource_config = config['resource']
  workers = resource_config.get('workers', {})

  if not workers:
    print("Error: No workers defined in config-resource.yaml")
    return 1

  # Generate compose file for each worker
  total_crs_count = 0
  for worker_name in workers.keys():
    print(f"\nProcessing worker: {worker_name}")

    # Get CRS list for this worker
    crs_list = get_crs_for_worker(worker_name, resource_config)

    if not crs_list:
      print(f"  No CRS instances configured for worker '{worker_name}', skipping...")
      continue

    print(f"  Found {len(crs_list)} CRS instance(s):")
    for crs in crs_list:
      print(f"    - {crs['name']}: CPUs={crs['cpus']}, Memory={crs['memory_limit']}")

    # Render compose file
    try:
      rendered = render_compose_for_worker(worker_name, crs_list, template_path,
                                           oss_fuzz_path, crs_path, args.project,
                                           args.fuzzer_command, config_dir)
    except FileNotFoundError as e:
      print(f"Error: {e}")
      return 1

    # Write to output file
    output_file = output_dir / f"compose-{worker_name}.yaml"
    output_file.write_text(rendered)
    print(f"  Written: {output_file}")

    total_crs_count += len(crs_list)

  print(f"\nSummary:")
  print(f"  Workers processed: {len(workers)}")
  print(f"  Total CRS instances: {total_crs_count}")
  print(f"  Output directory: {output_dir}")

  return 0


if __name__ == '__main__':
  exit(main())
