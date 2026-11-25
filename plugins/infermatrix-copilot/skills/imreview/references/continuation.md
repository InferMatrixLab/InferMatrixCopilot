# Continue a Direct review

A later explicit human authorization may request a separate durable added
allowance through `extend_direct_review` only when the connected server exposes
`direct-continuation/1`. Pass the immutable parent trace, exact head/context,
authorization ID/provenance, prior usage as host-declared or unknown, inherited
findings/gaps, remaining questions and stop condition. Keep the parent unchanged.
Read/replay the child with `resume_direct_review`; never restart `review` to reset
its budget. Before every command batch call `reserve_direct_commands` with a new
operation ID. Reservation retries reuse the ID; execution retries require a new
reservation. A crash never refunds reserved calls. Receipt reads do not establish
unused allowance, authenticated human intent or semantic completion. Reuse the
original containment receipt; a grant does not authorize unavailable knowledge.
If these operations/capability are absent, stop under the original ceiling.
