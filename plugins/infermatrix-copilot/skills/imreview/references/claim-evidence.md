# Verify claim evidence

Identify what the change promises at the affected public entry point. Check the
repository’s acceptance criteria and the workload, inputs, configuration and
revision covered by each claim. Prefer a targeted run when the environment and
review budget support it. Otherwise inspect the supplied output and raw records.
Attribute each observation to its run and report any untested paths.

| Claim | Evidence to inspect |
| --- | --- |
| Observable feature or UI behavior | Exercise the relevant interaction or inspect a capture tied to the reviewed build. A screenshot can support a visible state. A recording or trace can show a sequence or failure path. |
| Generated text, image, video or audio | Inspect the actual returned payload or decodable media and its inputs. Include the synthesis text for TTS and all claimed outputs for multimodal results. Check streaming chunks, alignment or stage traces where the claim depends on them. |
| Performance, quality or parity | Inspect both sides of a comparison, including applicable raw benchmark measurements and actual outputs. Match revisions, workload, device, model/checkpoint, input, seed and configuration where relevant. Explain differences that affect interpretation. |
| Campaign acceptance | Use the governing issue and frozen protocol. Trace the claim to its measured trials, retained failures and quality checks, then apply that campaign’s evidence and verdict rules. |

Open required artifacts or stable attachments and check their run identity. A hash
can check integrity but cannot show the behavior of an unavailable artifact.
Identify missing, inaccessible or mismatched records and name which claim remains
unverified. A reviewer run can supply a required artifact when policy permits it
and its actual output is retained and tied to the reviewed revision. A failed
local preflight limits reproduction even when contributor evidence is available.
