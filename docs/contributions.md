# Contribution scope and participation

[中文](contributions_zh.md) · [Home](../README.md) · [Repository workflow](../CONTRIBUTING.md)

## Responsibilities

Julius designed the experiments, product architecture and model architecture. Frontline coding, deployment and experiment execution are distinct work performed through the team's implementation process. Portfolio claims should name the actual role rather than imply sole implementation of the complete system.

The public edition contains team-authorized original source, documentation and selected results. Third-party components retain their notices. Names and affiliations should be added with permission and a concrete scope of work.

## First contribution

Choose a row in the [evidence guide](../evidence/README.md). Open its JSON and research case. Check the endpoint, comparison direction, sample unit and value. Open an Issue with the field path and correction or confirmation. For a documentation change, update both languages, open a PR and attach the record below. Maintainer review checks the claim against the cited data before merging and registering the outcome.

| Task | Deliverable | Acceptance | Claim status |
|---|---|---|---|
| Evidence review | Issue/PR record below | Exact field, units, arithmetic and limitation checked | Open; claim through an Issue |
| Clean mock installation | Environment and command log | Linux/WSL2 versions, installed locks, tests and one mock task recorded | Open; claim through an Issue |
| Objective explanation | Case 01 and notebook 01 improvement | Direct/QUBO/Ising conventions agree; example passes | Open; claim through an Issue |
| Product/algorithm adapter map | Source-linked design note and mock fixture | Input/output mapping and unsupported fields tested | Open; claim through an Issue |

## Contribution record

```markdown
### Question and scope
Claim or behavior inspected:
### Role
Experiment design / architecture / implementation / execution / review / documentation:
### Inputs and sources
Repository commit:
File and field or source passage:
Data rights / synthetic input:
### Method
OS and runtime versions:
Commands and mode (mock / local / external):
Comparison, sample unit and tolerance:
### Result
Expected:
Observed:
Output hash or public artifact:
Limitations and failed steps:
### Review and registration
Issue and PR:
Reviewer and decision:
Status/evidence documentation updated:
```

## Maintenance

Maintain both languages together, keep transformations traceable, and distinguish historical results from new runs. Handoffs link the Issue, branch, completed checks and next step. External execution changes require review of mode, permission boundary and cost-bearing action. Record upstream versions and retained licenses.
