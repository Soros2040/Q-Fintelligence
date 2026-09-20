[English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh-CN.md)


# Contributing

Start by reading the project question and the relevant chapter. Contributions should leave a claim, explanation or workflow easier to understand and check.

## Choose a first task

| Task | Deliverable | Acceptance criterion | Claim status |
| --- | --- | --- | --- |
| Reference review | A note under `contributions/` and a focused text correction | Original source, exact location, supported scope and reasoning | Open; claim through an Issue |
| Source walkthrough | A chapter improvement linked to a real module | Inputs, outputs and one example agree with the inspected version | Open; claim through an Issue |
| Bilingual review | Paired English/Chinese changes | Equations, numbers, tables, sources and status agree | Open; claim through an Issue |

## From an Issue to an accepted contribution

1. Open an Issue with the problem, proposed artifact and acceptance check. Link a first-task row if relevant.
2. Ask to claim the task in that Issue. The maintainer records the assignee and scope; a listing alone does not reserve it.
3. Create a branch in your fork. Keep one topic per change and update both languages together.
4. Submit a PR linking the Issue. Describe the source or environment, exact changes and checks actually performed.
5. Respond to review. The maintainer checks facts, ownership, scope, links and language parity before merging.
6. Add the PR/commit, reviewer and remaining work to the contribution record. A proposal becomes accepted only after review and merge.

Research changes include data provenance, version, configuration, seed/split, reproduction steps and results. Mark a check as unexecuted when it was only inspected. Use synthetic data for examples when real data cannot be redistributed. Never include credentials, private identities or unlicensed full texts.

<a id="contribution-record"></a>
## Contribution record

Copy this into a new Markdown note under `contributions/`:

```text
Title:
Issue and agreed scope:
Author and actual contribution:
Source and exact section / table / code location:
Version / commit / artifact hash:
Environment and reproduction steps (if applicable):
Finding and supporting evidence:
Checks performed and their results:
English / Chinese pages updated:
Reviewer and decision:
Merged PR / commit:
Remaining question:
```

Do not fill in a reviewer, acceptance decision or run result before it exists. See [maintenance](docs/maintenance.md) for handoff responsibilities.

## License

Contribute only material you can license. Original code uses MIT and original documentation uses CC BY-NC-SA 4.0 unless a file states its preserved upstream license. Keep third-party attribution and identify reused content.

Worked example: [a source review record](contributions/example-review.md).
