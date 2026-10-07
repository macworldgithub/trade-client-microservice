"""
ERA Orchestrator
Coordinates ERA modules, Customer lookup, Quote creation, and PartsCheck verification.
"""

import sys
import os
import json
import logging
from Era_customer import EraCustomer
from Era_power import EraPower
from Era_quote import EraQuote
from Era_supplier import EraSupplier
from parts_finder import PartsFinder
from partscheck import PartsCheck

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class EraOrchestrator:
    def __init__(self, config_file: str = None):
        self.config_file = config_file or os.path.join(os.path.dirname(__file__), "era_config.json")
        self.config = self._load_config()
        self.customer = EraCustomer(self.config)
        self.power = EraPower(self.config)
        self.quote = EraQuote(self.config)
        self.supplier = EraSupplier(self.config)
        self.finder = PartsFinder(self.config)
        self.checker = PartsCheck(self.config)

    def _load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def run_flow(self, customer_id: str, quote_data: dict):
        logger.info(f"Starting orchestration flow for Customer: {customer_id}")
        cust = self.customer.get_customer(customer_id)
        quote_res = self.quote.process_quote(quote_data)
        return {
            "customer": cust,
            "quote": quote_res,
            "status": "success"
        }


if __name__ == "__main__":
    orchestrator = EraOrchestrator()
    res = orchestrator.run_flow("CUST-1001", {"quote_id": "Q-999", "items": [{"part": "OIL-01"}]})
    print(json.dumps(res, indent=2))
