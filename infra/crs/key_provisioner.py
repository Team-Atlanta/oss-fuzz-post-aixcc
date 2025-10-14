#!/usr/bin/env python3
"""
LiteLLM Key Provisioner Service

This service provisions API keys for LiteLLM proxy by making requests to the
LiteLLM proxy's key management endpoints.
"""

import os
import sys
import time
import logging
import requests
import argparse
import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List

# Configure logging
logging.basicConfig(
  level=logging.INFO,
  format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class LiteLLMKeyProvisioner:
  """Handles provisioning of LiteLLM API keys."""
  
  def __init__(self, litellm_url: str, master_key: str):
    """
    Initialize the key provisioner.
    
    Args:
      litellm_url: Base URL of the LiteLLM proxy service
      master_key: Master key for authenticating with LiteLLM proxy
    """
    self.litellm_url = litellm_url.rstrip('/')
    self.master_key = master_key
    self.session = requests.Session()
    self.session.headers.update({
      'Authorization': f'Bearer {master_key}',
      'Content-Type': 'application/json'
    })
  
  def health_check(self) -> bool:
    """
    Check if the LiteLLM proxy service is healthy.
    
    Returns:
      True if service is healthy, False otherwise
    """
    try:
      response = self.session.get(f"{self.litellm_url}/health", timeout=10)
      if response.status_code == 200:
        logger.info("LiteLLM proxy service is healthy")
        return True
      else:
        logger.warning(f"LiteLLM proxy health check failed: {response.status_code}")
        return False
    except requests.exceptions.RequestException as e:
      logger.error(f"Failed to connect to LiteLLM proxy: {e}")
      return False
  
  def generate_key(self, user_id: str, max_budget: Optional[float] = None,
                  models: Optional[list] = None, duration: Optional[str] = None,
                  tpm_limit: Optional[int] = None, rpm_limit: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Generate a new API key for a user.

    Args:
      user_id: Unique identifier for the user
      max_budget: Maximum budget for the key (optional)
      models: List of allowed models (optional)
      duration: Key duration (optional)
      tpm_limit: Tokens per minute limit (optional)
      rpm_limit: Requests per minute limit (optional)

    Returns:
      Dictionary containing key information if successful, None otherwise
    """
    payload = {
      "user_id": user_id
    }

    if max_budget is not None:
      payload["max_budget"] = max_budget
    if models is not None:
      payload["models"] = models
    if duration is not None:
      payload["duration"] = duration
    if tpm_limit is not None:
      payload["tpm_limit"] = tpm_limit
    if rpm_limit is not None:
      payload["rpm_limit"] = rpm_limit
    
    try:
      response = self.session.post(
        f"{self.litellm_url}/key/generate",
        json=payload,
        timeout=30
      )
      
      if response.status_code == 200:
        key_data = response.json()
        logger.info(f"Successfully generated key for user {user_id}")
        return key_data
      else:
        logger.error(f"Failed to generate key: {response.status_code} - {response.text}")
        return None
        
    except requests.exceptions.RequestException as e:
      logger.error(f"Request failed while generating key: {e}")
      return None
  
  def store_key(self, user_id: str, key_data: Dict[str, Any]) -> bool:
    """
    Store the generated key to a file.

    Args:
      user_id: User identifier
      key_data: Key data from LiteLLM API

    Returns:
      True if successful, False otherwise
    """
    try:
      # The volume is already mounted at /keys/<user_id>, so use that directly
      keys_dir = Path("/keys") / user_id

      # Ensure directory exists and is writable
      if not keys_dir.exists():
        logger.error(f"Keys directory does not exist: {keys_dir}")
        return False

      # Store the key in a file
      key_file = keys_dir / "api_key"
      with open(key_file, 'w') as f:
        f.write(key_data.get('key', ''))

      # Store metadata
      metadata_file = keys_dir / "metadata.json"
      with open(metadata_file, 'w') as f:
        import json
        json.dump(key_data, f, indent=2)

      logger.info(f"Stored key for user {user_id} in {key_file}")
      return True

    except Exception as e:
      logger.error(f"Failed to store key for user {user_id}: {e}")
      return False


def load_config(config_dir: str) -> Dict[str, Any]:
  """Load all configuration files from config directory."""
  config = {}

  # Load resource configuration
  resource_config_path = Path(config_dir) / "config-resource.yaml"
  try:
    with open(resource_config_path, 'r') as f:
      config['resource'] = yaml.safe_load(f)
    logger.info(f"Loaded resource configuration from {resource_config_path}")
  except Exception as e:
    logger.error(f"Failed to load resource configuration from {resource_config_path}: {e}")
    sys.exit(1)

  # Load CRS configuration (optional)
  crs_config_path = Path(config_dir) / "config-crs.yaml"
  if crs_config_path.exists():
    try:
      with open(crs_config_path, 'r') as f:
        config['crs'] = yaml.safe_load(f)
      logger.info(f"Loaded CRS configuration from {crs_config_path}")
    except Exception as e:
      logger.warning(f"Failed to load CRS configuration from {crs_config_path}: {e}")
      config['crs'] = {}
  else:
    logger.info("No CRS configuration file found, using defaults")
    config['crs'] = {}

  return config


def calculate_budgets(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
  """
  Calculate budget allocations for each CRS.

  Args:
    config: Configuration dictionary containing 'resource' and 'crs' keys

  Returns:
    Dictionary mapping CRS name to budget configuration
  """
  resource_config = config.get('resource', {})
  crs_configs = resource_config.get('crs', {})
  global_llm = resource_config.get('llm', {})

  # Get global budget settings
  global_max_budget = global_llm.get('max_budget', 100)
  global_max_rpm = global_llm.get('max_rpm')
  global_max_tpm = global_llm.get('max_tpm')

  # Collect all CRS names
  all_crs = list(crs_configs.keys())

  if not all_crs:
    logger.warning("No CRS configurations found")
    return {}

  # Calculate budgets
  budgets = {}
  total_allocated_budget = 0
  crs_with_budget = []
  crs_without_budget = []

  # First pass: identify CRS with explicit budgets
  for crs_name, crs_config in crs_configs.items():
    crs_llm = crs_config.get('llm', {})

    if 'max_budget' in crs_llm:
      # CRS has explicit budget
      budgets[crs_name] = {
        'max_budget': crs_llm['max_budget'],
        'max_rpm': crs_llm.get('max_rpm', global_max_rpm),
        'max_tpm': crs_llm.get('max_tpm', global_max_tpm)
      }
      total_allocated_budget += crs_llm['max_budget']
      crs_with_budget.append(crs_name)
    else:
      crs_without_budget.append(crs_name)

  # Second pass: divide remaining budget among CRS without explicit budgets
  if crs_without_budget:
    remaining_budget = global_max_budget - total_allocated_budget
    if remaining_budget < 0:
      logger.warning(f"Total allocated budget ({total_allocated_budget}) exceeds global budget ({global_max_budget})")
      remaining_budget = 0

    per_crs_budget = remaining_budget / len(crs_without_budget) if crs_without_budget else 0

    for crs_name in crs_without_budget:
      crs_llm = crs_configs[crs_name].get('llm', {})
      budgets[crs_name] = {
        'max_budget': per_crs_budget,
        'max_rpm': crs_llm.get('max_rpm', global_max_rpm),
        'max_tpm': crs_llm.get('max_tpm', global_max_tpm)
      }

  # Log budget allocation
  logger.info("Budget allocation:")
  for crs_name, budget in budgets.items():
    logger.info(f"  {crs_name}: budget=${budget['max_budget']:.2f}, "
                f"rpm={budget['max_rpm']}, tpm={budget['max_tpm']}")

  return budgets


def get_models_list(config: Dict[str, Any], crs_name: str) -> List[str]:
  """
  Get list of allowed models for a specific CRS from CRS configuration.

  Args:
    config: Configuration dictionary containing 'crs' key
    crs_name: Name of the CRS to get models for

  Returns:
    List of model names for the specified CRS
  """
  crs_config = config.get('crs', {})
  crs_specific = crs_config.get(crs_name, {})
  models = crs_specific.get('models', [])
  logger.info(f"Using models for {crs_name}: {models}")
  return models


def parse_arguments():
  """Parse command line arguments."""
  parser = argparse.ArgumentParser(description='Provision LiteLLM API keys for CRS users')
  parser.add_argument(
    '--config-dir',
    type=str,
    required=True,
    help='Path to configuration directory'
  )
  parser.add_argument(
    '--duration',
    type=str,
    default=None,
    help='Key duration (optional, e.g., 30d, 7d, 1h)'
  )
  return parser.parse_args()


def main():
  """Main function to run the key provisioner service."""
  # Parse command line arguments
  args = parse_arguments()

  # Load configuration
  config = load_config(args.config_dir)

  # Calculate budget allocations
  budgets = calculate_budgets(config)

  if not budgets:
    logger.error("No CRS configurations found, nothing to provision")
    sys.exit(1)

  # Get configuration from environment variables
  litellm_url = os.getenv('LITELLM_URL', 'http://litellm:4000')
  master_key = os.getenv('LITELLM_MASTER_KEY')

  if not master_key:
    logger.error("LITELLM_MASTER_KEY environment variable is required")
    sys.exit(1)

  # Initialize the provisioner
  provisioner = LiteLLMKeyProvisioner(litellm_url, master_key)

  # Wait for LiteLLM service to be ready
  logger.info("Waiting for LiteLLM proxy service to be ready...")
  max_retries = 30
  retry_count = 0

  while retry_count < max_retries:
    if provisioner.health_check():
      break
    retry_count += 1
    logger.info(f"Retry {retry_count}/{max_retries} - waiting 10 seconds...")
    time.sleep(10)
  else:
    logger.error("LiteLLM proxy service is not ready after maximum retries")
    sys.exit(1)

  # Generate keys for all CRS
  successful_keys = 0
  failed_keys = 0

  for crs_name, budget_config in budgets.items():
    logger.info(f"Generating key for CRS: {crs_name}")

    # Get models list for this specific CRS
    models = get_models_list(config, crs_name)

    key_data = provisioner.generate_key(
      user_id=crs_name,
      max_budget=budget_config['max_budget'],
      models=models,
      duration=args.duration,
      tpm_limit=budget_config['max_tpm'],
      rpm_limit=budget_config['max_rpm']
    )

    if key_data:
      if provisioner.store_key(crs_name, key_data):
        successful_keys += 1
        logger.info(f"Successfully provisioned and stored key for CRS {crs_name}")
      else:
        failed_keys += 1
        logger.error(f"Failed to store key for CRS {crs_name}")
    else:
      failed_keys += 1
      logger.error(f"Failed to generate key for CRS {crs_name}")

  logger.info(f"Key provisioning completed: {successful_keys} successful, {failed_keys} failed")

  if failed_keys > 0:
    sys.exit(1)


if __name__ == "__main__":
  main()
