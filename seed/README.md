# DocFlow AI — Demo Seed Environment

This module provides an idempotent, automated seed script for populating DocFlow AI with the **Lumen Retail** demonstration organization, project **Product Launch Q1**, 7 realistic users with distinct ABAC roles, and 7 documents ingested through the authentic document pipeline.

---

## Quick Start

Run from the `meem_salvage` root directory using the project virtual environment:

### 1. Seed the Database
Populates the organization, project, stages, users, and documents:
```bash
python -m seed.seed_demo
```
*Note: This command is fully idempotent. Running it repeatedly will skip existing records and run the verification suite in seconds.*

### 2. Verify State
Runs the comprehensive 16-point verification suite without modifying any data:
```bash
python -m seed.seed_demo --verify
```

### 3. Clean Reset & Rebuild
Wipes all Lumen Retail database rows and its Qdrant collection, then re-seeds from scratch:
```bash
python -m seed.seed_demo --reset
```
*Note: The reset operation is strictly scoped to the `Lumen Retail` tenant (`10000000-0000-0000-0000-000000000001`) and never touches any other tenant's data.*

---

## Seeded Entities

### Organization & Project
* **Tenant**: `Lumen Retail` (`10000000-0000-0000-0000-000000000001`)
* **Project**: `Product Launch Q1`
* **Teams**:
  * `Engineering`
  * `Marketing`
  * `Leadership`
* **Stages** (in order):
  1. `Planning` (`requires_approval = False`)
  2. `Design` (`requires_approval = False`)
  3. `Development` (`requires_approval = False`)
  4. `Testing` (`requires_approval = True`)
  5. `Release` (`requires_approval = True`)

### Stage Whitelist (`TeamStageAccess`)
* **Planning, Design, Development**: Open to `Engineering`, `Marketing`, and `Leadership`.
* **Testing**: Open to `Engineering` and `Marketing`.
* **Release**: Whitelist is empty (restricted to `project_admin` and `org_admin` only).

---

## Users & Credentials

All seeded accounts use password: **`DemoPass123!`** (hashed via PBKDF2-HMAC-SHA256).

| User | Email | Scope / Roles | Notes |
|---|---|---|---|
| **Meera Kapoor** | `meera.kapoor@lumenretail.com` | Engineering `team_lead`, Marketing `viewer` | Demo persona on camera |
| **Arjun Verma** | `arjun.verma@lumenretail.com` | Engineering `contributor` | Document author (Docs 1, 4, 6) |
| **Divya Shah** | `divya.shah@lumenretail.com` | Marketing `contributor`, Engineering `contributor` | Document author (Docs 2, 5) |
| **Vikram Nair** | `vikram.nair@lumenretail.com` | Leadership `team_lead`, Marketing `team_lead` | Owns Doc 3, approves Marketing confidential access |
| **Sneha Rao** | `sneha.rao@lumenretail.com` | `project_admin` on Product Launch Q1 | Owns Release stage, uploads Doc 7 |
| **Rohan Iyer** | `rohan.iyer@lumenretail.com` | `is_org_admin = True` | Company org admin |
| **Abhardwaj** | `blabhardwaj@gmail.com` | `is_org_admin = True`, `project_admin` | Google OAuth administrative sign-in |

---

## Documents Ingested

Documents are read from `docs for demo/` and processed through the authentic application pipeline (`POST /documents/upload-file`, `POST /documents/review/message`, and workflow endpoints):

| # | File | Stage | Team | Sensitivity | State | Author | Qdrant Indexed |
|---|---|---|---|---|---|---|---|
| 1 | `01-checkout-redesign-requirements.md` | Planning | Engineering | internal | approved | Arjun Verma | Yes |
| 2 | `02-q1-launch-gtm-plan.md` | Planning | Marketing | internal | approved | Divya Shah | Yes |
| 3 | `03-executive-pricing-strategy.md` | Planning | Marketing | confidential | approved | Vikram Nair | Yes |
| 4 | `04-checkout-payment-integration-spec.md` | Development | Engineering | internal | approved | Arjun Verma | Yes |
| 5 | `05-mobile-compatibility-test-plan.md` | Testing | Engineering | internal | approved | Divya Shah | Yes |
| 6 | `06-payment-gateway-failover-test-plan.md` | Testing | Engineering | internal | **submitted** | Arjun Verma | **No** (0 points) |
| 7 | `07-release-readiness-checklist.md` | Release | Engineering | internal | approved | Sneha Rao | Yes |

* `08-ROUGH-search-filter-test-plan.md` is preserved on disk for live pasting in the demo.
