"""Complete approved read-only work without a source publication or merge."""

from __future__ import annotations

from .common import Blocked, short_summary
from .pipelines import task_pipeline
from .policy import now, require


def notice(gh, number, state):
    marker = f"<!-- nexkit:task-complete:{state['run_key']} -->"
    if any(marker in (item.get("body") or "") for item in gh.comments(number)):
        return
    summaries = state.get("task_result", [])
    body = marker + "\n**Work completed**\n\n"
    for item in summaries[:20]:
        body += f"- **{item['name']}**: {short_summary(item['summary'], 800)}\n"
    if len(summaries) > 20:
        body += f"\n{len(summaries) - 20} more results are recorded in `nexkit status`.\n"
    run = state.get("approval_execution", {}).get("run_key", state["run_key"]).split(".")[0]
    body += f"\n[View the workflow run](https://github.com/{gh.repository}/actions/runs/{run}). "
    runs = sorted({item.get("execution", state["run_key"]).split(".")[0] for item in summaries})
    if runs:
        body += (
            "Result artifacts: "
            + ", ".join(
                f"[run {value}](https://github.com/{gh.repository}/actions/runs/{value})"
                for value in runs
            )
            + ". "
        )
    body += "This issue remains open for follow-up."
    gh.comment(number, body)


def finish(gh, context, *, jobs_succeeded):
    from .approvals import ApprovalRequired, record_denial, require_complete
    from .delivery import failed, revalidate
    from .steps import require_results

    require(task_pipeline(context["config"]), "Task completion requires a tasks entrypoint")
    number = context["issue"]["number"]
    state, _ = gh.get_state(number)
    require(state.get("run_key") == context["run_key"], "Cannot finalize another work round")
    if state.get("status") == "completed":
        notice(gh, number, state)
        return state
    if state.get("status") == "waiting_for_approval":
        return state
    try:
        state, revision = revalidate(gh, context)
        require(jobs_succeeded, "A required native job failed or was skipped")
        require_results(state, context)
        require_complete(gh, context, state)
        summaries = []
        prefix = context["run_key"] + "/"
        for group in ("invocations", "steps"):
            for key, record in state.get(group, {}).items():
                if key.startswith(prefix):
                    summaries.append(
                        {
                            "name": key[len(prefix) :],
                            "summary": record["summary"],
                            "execution": record.get("execution", context["run_key"]),
                        }
                    )
        require(summaries, "No managed work was completed")
        state.update(
            status="completed",
            task_result=summaries,
            activity=short_summary(f"Completed: {context['issue']['title']}"),
            feedback=None,
            updated_at=now(),
        )
        state.pop("reason", None)
        gh.save_state(number, state, revision)
    except Blocked as exc:
        if isinstance(exc, ApprovalRequired):
            record_denial(gh, context, exc)
        return failed(gh, context, str(exc))
    notice(gh, number, state)
    return state
