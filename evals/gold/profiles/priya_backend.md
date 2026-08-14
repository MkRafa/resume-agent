# Priya Raghavan

priya.raghavan@example.com | +91 98765 43210 | Bengaluru, India
github.com/example-priya | linkedin.com/in/example-priya

Backend engineer, 6 years, distributed systems and payments infrastructure.

## Experience

### Senior Backend Engineer — Northwind Payments (Mar 2022 – Present)
- Owned the settlement service that reconciles ~2M transactions/day across 4 payment processors.
- Cut p99 settlement latency from 1.8s to 640ms by replacing the synchronous ledger write with a Kafka-backed write-behind queue.
- Led the migration of 14 services from self-managed EC2 to EKS; wrote the Terraform modules the platform team still uses.
- Mentored 3 junior engineers; two were promoted within 18 months.
- Introduced contract testing (Pact) across 9 service boundaries, reducing integration incidents by roughly 60%.

### Backend Engineer — Kestrel Logistics (Jul 2019 – Feb 2022)
- Built the route-optimisation API in Python/FastAPI serving 40k requests/day to the dispatch app.
- Helped migrate the monolith's shipment module into a standalone service (team of 5; I owned the data migration).
- Added Postgres partitioning to the events table, taking a nightly report from 45 min to under 4 min.
- On-call rotation; wrote the runbooks for the shipment and billing services.

## Projects
- **ledger-lint** — open-source static analyser for double-entry bookkeeping schemas. 400+ GitHub stars.

## Education
B.E. Computer Science, R.V. College of Engineering, 2019

## Skills
Python, Go (basic), FastAPI, Postgres, Kafka, Redis, Docker, Kubernetes, Terraform, AWS, gRPC, pytest
