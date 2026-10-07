"""
PartsCheck Automation Orchestration Script
"""

import sys
import os
import json
import logging
from PartscheckAuto import PartsCheckAuto

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def run_automation(input_file: str = None):
    logger.info("Starting PartsCheck Automation run...")
    checker = PartsCheckAuto()
    
    if input_file and os.path.exists(input_file):
        with open(input_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            order_id = data.get("order_id", "default")
            candidates = data.get("candidates", [])
            res = checker.check_parts(order_id, candidates)
            logger.info(f"Result: {res}")
            return res
    else:
        logger.info("No input file specified, running with defaults.")
        return checker.check_parts("DEMO-001", [{"part_number": "12345-ABC"}])


if __name__ == "__main__":
    file_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run_automation(file_arg)
