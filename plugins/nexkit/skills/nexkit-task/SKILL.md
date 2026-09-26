---
name: nexkit-task
description: Perform an accepted read-only task within a NexKit work item, such as investigating code or preparing evidence for a later agent invocation.
---

# NexKit read-only task

Read the accepted task, approved issue and relevant source or project knowledge.
Use the supplied consumer skills and inspect actual files with tools. Treat
previous agent outputs as claims to check against the source. Keep findings
within the approved requirement and describe unresolved decisions explicitly.

Return useful findings, file references and evidence in `summary`, together with
the commands actually used, limitations and installed skills used. Distinguish
observations from proposals. If the task cannot be completed within its scope
or available tools, return `blocked` and the concrete missing condition.

This session cannot edit source or control files. Its output supplies context to
subsequent jobs; it does not approve a requirement, independently approve code,
grant permissions or authorize merge/release. Return the supplied JSON schema
and include `nexkit-task` in `skills_used`.
