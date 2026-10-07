"""
ERA Orchestrator Module
Coordinates workflow between PartsCheck incoming RFQs and Pentana eraPower Web portal.

Workflow:
1. Receives Quote / RFQ data from PartsCheck (VIN, repairer name, requested parts).
2. Matches repairer with Pentana eraPower trade account.
3. Retrieves stock availability (Dandenong, Cheltenham, Cranbourne, etc.), bin locations, trade prices, and ETA from eraPower.
4. Generates formatted quotation payloads and updates result JSON artifacts.
"""

import sys
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

from Era_customer import EraCustomer
from Era_power import EraPower
from Era_quote import EraQuote
from Era_supplier import EraSupplier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("EraOrchestrator")


class EraOrchestrator:
    def __init__(self, config_file: Optional[str] = None):
        self.config_file = config_file or os.path.join(os.path.dirname(__file__), "era_config.json")
        self.config = self._load_config()
        self.customer = EraCustomer(self.config)
        self.power = EraPower(self.config)
        self.quote = EraQuote(self.config)
        self.supplier = EraSupplier(self.config)

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load config from {self.config_file}: {e}")
        return {}

    async def process_partscheck_rfq(
        self,
        quote_id: str,
        vin: str,
        repairer: str,
        parts: List[Dict[str, Any]],
        deadline: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Process an incoming quote request from PartsCheck against Pentana eraPower.
        """
        logger.info(f"==> [EraOrchestrator] Processing RFQ #{quote_id} | VIN: {vin} | Repairer: {repairer}")

        # 1. Match customer account in eraPower
        customer_info = await self.power.lookup_account(repairer)
        account_code = customer_info.get("account_code", "UNKNOWN")

        # 2. Check stock & price for each requested item
        quoted_lines = []
        all_available = True

        for idx, part in enumerate(parts):
            desc = part.get("description", "")
            part_no = part.get("part_number") or f"PART-{idx+100}"
            qty = int(part.get("quantity", 1))

            part_data = await self.power.lookup_part(part_no, account_code=account_code)
            
            quoted_lines.append({
                "line_index": idx + 1,
                "requested_description": desc,
                "resolved_part_number": part_data["part_number"],
                "quantity": qty,
                "list_price": part_data["list_price"],
                "trade_price": part_data["trade_price"],
                "total_trade_amount": round(part_data["trade_price"] * qty, 2),
                "stock_branches": part_data["branches"],
                "total_available_stock": part_data["total_available_stock"],
                "status": "QUOTED" if part_data["total_available_stock"] >= qty else "BACKORDER_OR_TRANSFER"
            })

            if part_data["total_available_stock"] < qty:
                all_available = False

        # 3. Compile overall quotation structure
        quotation = {
            "quote_id": quote_id,
            "vin": vin,
            "repairer": repairer,
            "deadline": deadline,
            "customer_account": customer_info,
            "quoted_lines": quoted_lines,
            "all_in_stock": all_available,
            "status": "READY_FOR_PARTSCHECK_SUBMISSION" if customer_info.get("credit_status") == "CLEAR" else "CONTROLLER_EXCEPTION_QUEUE",
            "created_at": datetime.utcnow().isoformat() + "Z"
        }

        # 4. Save results into structured artifact files in project root
        self._save_results(quotation, customer_info)

        logger.info(f"<== [EraOrchestrator] Completed RFQ #{quote_id} with {len(quoted_lines)} items. Status: {quotation['status']}")
        return quotation

    def _save_results(self, quotation: Dict[str, Any], customer_info: Dict[str, Any]):
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        
        try:
            with open(os.path.join(root_dir, "era_results.json"), "w", encoding="utf-8") as f:
                json.dump(quotation, f, indent=2)

            with open(os.path.join(root_dir, "era_customer_result.json"), "w", encoding="utf-8") as f:
                json.dump(customer_info, f, indent=2)

            with open(os.path.join(root_dir, "era_quote_result.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "quote_id": quotation["quote_id"],
                    "status": quotation["status"],
                    "total_lines": len(quotation["quoted_lines"]),
                    "timestamp": quotation["created_at"]
                }, f, indent=2)

        except Exception as e:
            logger.error(f"Failed writing results JSON: {e}")


if __name__ == "__main__":
    import asyncio
    orchestrator = EraOrchestrator()
    sample_parts = [
        {"description": "FRONT BUMPER COVER", "part_number": "52119-0D900", "quantity": 1},
        {"description": "HEADLAMP RH", "part_number": "81110-0D350", "quantity": 1}
    ]
    res = asyncio.run(orchestrator.process_partscheck_rfq("17814736", "MR0HA3CD600000000", "ABC Smash Repairs", sample_parts))
    print(json.dumps(res, indent=2))
