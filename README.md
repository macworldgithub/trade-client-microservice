# Trade Client Microservice (ERA & PartsCheck Integration)

Microservice for Trade Client automotive parts verification, ERA quotation management, and automated catalog matching.

## Architecture

- **Backend Service (TypeScript / NestJS)**: REST API gateway managing auth, users, notifications, and orchestration triggers.
- **Python Automation Engine**:
  - `PartsCheckAuto.py` / `Partscheckautomation.py`: Microcat & PartsCheck validation.
  - `Era_orchestrator.py`: ERA quote creation, supplier verification, customer lookup.
  - `Era_power.py`, `Era_customer.py`, `Era_quote.py`, `Era_supplier.py`: Modular ERA adapters.

## Project Structure

```text
├── Parts_Check/
│   ├── microcat_candidates_*.json
│   ├── PartscheckAuto.py
│   └── Partscheckautomation.py
├── src/
│   ├── auth/
│   ├── mail/
│   ├── users/
│   ├── app.module.ts
│   ├── main.ts
│   ├── era_config.json
│   ├── Era_customer.py
│   ├── Era_orchestrator.py
│   ├── Era_power.py
│   ├── Era_quote.py
│   ├── Era_supplier.py
│   ├── parts_finder.py
│   └── partscheck.py
├── test/
│   ├── app.e2e-spec.ts
│   └── jest-e2e.json
├── package.json
├── tsconfig.json
└── requirements.txt
```

## Setup & Installation

### TypeScript / NestJS Service
```bash
npm install
npm run start:dev
```

### Python Automation
```bash
python -m venv venv
# On Windows
.\venv\Scripts\activate
# On Linux/macOS
source venv/bin/activate

pip install -r requirements.txt
```

## Running ERA Orchestrator
```bash
python src/Era_orchestrator.py
```
