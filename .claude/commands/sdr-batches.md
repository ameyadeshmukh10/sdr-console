---
description: Batch the HubSpot ICP contacts (25/batch), dispatch batch-runner sub-agents in parallel to generate value-anchored meeting-CTA copy, then enroll into Bison.
argument-hint: "[N batches | all] [enroll]"
allowed-tools: Bash, Task
---

Run the SDR outbound batch pipeline. Arguments: `$ARGUMENTS`
- First token = how many pending batches to process this run (a number, or `all`). Default `all` if omitted.
- Include the word `enroll` anywhere to push live into Bison; otherwise only dry-run the enrollment.

Execute exactly these steps with minimal commentary (do NOT re-plan or read knowledge files yourself —
the sub-agents do generation):

1. `python3 .claude/skills/sdr-pipeline/scripts/sdr_batches.py init` then `python3 .claude/skills/sdr-pipeline/scripts/sdr_batches.py status`.
2. Get pending batch ids. If the first argument is a number N: `python3 .claude/skills/sdr-pipeline/scripts/sdr_batches.py pending-batches --limit N`. If `all` or omitted: `python3 .claude/skills/sdr-pipeline/scripts/sdr_batches.py pending-batches`.
3. If there are no pending batches, skip to step 5.
4. Dispatch one **sdr-batch-runner** sub-agent per pending batch id, **in parallel up to 8 at a time** (multiple Task tool calls in a single message). Each sub-agent prompt is just: `Process batch <id>.` After a wave finishes, dispatch the next wave until every selected batch is processed.
5. `python3 .claude/skills/sdr-pipeline/scripts/sdr_batches.py status`.
6. Enrollment:
   - Always first: `python3 .claude/skills/sdr-pipeline/scripts/sdr_batches.py enroll --dry-run`.
   - Only if the arguments contain `enroll`: then run `python3 .claude/skills/sdr-pipeline/scripts/sdr_batches.py enroll` (live writes to Bison, routed per persona to campaigns 10/11/12/13).
7. Print a short final summary: batches processed this run, contacts generated / failed / enrolled (from `status`).
