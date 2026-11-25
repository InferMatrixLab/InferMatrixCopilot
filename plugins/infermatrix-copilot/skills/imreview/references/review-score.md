# Advisory review score

Score only on request, after the normal review. Use the same pinned evidence and
give each gap a single primary dimension. Decide applicability from the scope,
claims and governing requirements before examining the available artifacts.

| Dimension | Weight | Assess |
| --- | ---: | --- |
| Scope and intent | 15 | Fit to the accepted goal, boundaries and affected entry points. |
| Correctness and safety | 25 | Reachable behavior, compatibility, resource ownership and failure handling. |
| Validation | 20 | Coverage of changed paths by relevant tests, CI and runtime checks. |
| Claim evidence | 30 | Actual outputs and comparable raw records supporting applicable claims. |
| Simplicity | 10 | Necessity of added abstractions, reuse and avoidable state or duplication. |

Rate each applicable dimension from 0 to 3. Use 3 for matching evidence across
the applicable checks, 2 for a bounded gap, 1 for a material gap with some
support, and 0 for no usable evidence or a confirmed material contract violation.
Use `N/A` only for a dimension outside the scope. An in-scope, unassessed
dimension stays unresolved.

* Give Claim evidence **0/3** when any required output or raw benchmark record
  is missing, inaccessible or tied to the wrong run. Summary metrics, hashes
  and green CI cannot fill that gap.
* If the applicable repository policy zeroes Simplicity for added or rewritten
  explanatory comments, apply **0/3** to that dimension and cite the rule.
  Honor its scope and exemptions, such as license headers, shebangs, tool
  directives and required docstrings. Elsewhere assess comments on their merits.

Compute `100 × sum(weight × rating) / (3 × sum(applicable weights))` and
round once to the nearest integer, with halves up. For example, if all other
ratings are 3, missing required claim evidence yields **70/100**. A docs-only
change with no applicable claim and the same remaining ratings yields **100/100**
over 70 applicable weight points.

Withhold the total if the snapshot is stale, any applicable dimension is
unassessed, or a material part of the diff could not be reviewed. Name the gap
and report supported ratings. Otherwise report the reviewed base/head (or local
snapshot), total, applicable weight, each rating with its evidence source and the
highest-priority remaining action. Keep repository review and campaign verdicts
independent of the numeric score.
