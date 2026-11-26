import unittest

from tests.answers import NAMED, real_sources, unanswered_writes

# The part of `host` that every case shares: `step` is where an engine answer comes from.
HOST = "from . import engine\ndef step(item):\n    return engine.answer(item)\n"


def found(**modules):
    """The functions reported for the synthetic modules, with `host` beside them."""
    return unanswered_writes({"host": HOST, **modules}, named={})


class NoWriteWithoutTheEngine(unittest.TestCase):
    """No module outside `nexkit/engine/` starts, merges, updates, closes, cancels or reruns
    anything with no engine answer before it, as docs/engine.md#getting-there says."""

    def test_every_write_of_nexkit_follows_an_engine_answer(self):
        self.assertEqual(unanswered_writes(real_sources()), [])

    def test_the_retry_of_a_stage_with_no_engine_call_before_it_is_found_in_the_real_code(self):
        modules = real_sources()
        call = "answer = host.stage_transient(cfg, decision, now, error, label)"
        self.assertIn(call, modules["report"])
        modules["report"] = modules["report"].replace(call, "answer = ('retry',)")
        self.assertEqual(unanswered_writes(modules), ["cli.cmd_report"])

    def test_the_modules_read_are_those_outside_the_engine(self):
        modules = real_sources()
        for name in ("autopilot", "host", "maintain", "report", "route", "cli", "runtimes.base"):
            self.assertIn(name, modules)
        self.assertFalse([name for name in modules if name.startswith("engine")])
        self.assertNotIn("github", modules)

    def test_every_named_write_is_a_function_of_nexkit_with_a_reason(self):
        from tests.answers import Index

        functions = Index(real_sources()).functions
        for name, reason in NAMED.items():
            self.assertIn(name, functions)
            self.assertTrue(reason)

    def test_a_write_in_a_named_function_is_not_reported(self):
        source = "def sweep(gh):\n    gh.dispatch('w', 'main', {})\n"
        self.assertEqual(found(auto=source), ["auto.sweep"])
        self.assertEqual(
            unanswered_writes({"host": HOST, "auto": source}, named={"auto.sweep": "no item"}), []
        )

    def test_a_function_of_the_same_name_in_another_module_is_not_named(self):
        # A function of the same name in another module is not named.
        source = "def request(gh):\n    gh.dispatch('w', 'main', {})\n"
        self.assertEqual(unanswered_writes({"other": source}), ["other.request"])


class WritesWithNoAnswer(unittest.TestCase):
    def test_a_dispatch_with_no_engine_call_is_reported(self):
        source = "def bad(gh):\n    gh.dispatch('w', 'main', {})\n"
        self.assertEqual(found(mod=source), ["mod.bad"])

    def test_a_merge_with_no_engine_call_is_reported(self):
        source = "def bad(gh, pr, head):\n    return gh.merge(pr, head)\n"
        self.assertEqual(found(mod=source), ["mod.bad"])

    def test_a_close_with_no_engine_call_is_reported(self):
        source = (
            "def bad(gh):\n    gh.close_issue(1, 'completed')\n"
            "def worse(gh):\n    gh.close_pull(2)\n"
        )
        self.assertEqual(found(mod=source), ["mod.bad", "mod.worse"])

    def test_a_branch_update_a_cancel_and_a_rerun_are_reported(self):
        source = (
            "def update(gh):\n    gh.update_branch(1, 'a')\n"
            "def cancel(gh):\n    gh.cancel_run(1)\n"
            "def rerun(gh):\n    gh.rerun_failed_jobs(1)\n"
        )
        self.assertEqual(found(mod=source), ["mod.cancel", "mod.rerun", "mod.update"])

    def test_a_write_after_the_engine_call_passes(self):
        source = (
            "from . import host\n"
            "def good(gh, item):\n"
            "    host.step(item)\n"
            "    gh.dispatch('w', 'main', {})\n"
            "    gh.merge(1, 'a')\n"
            "    gh.close_issue(1)\n"
        )
        self.assertEqual(found(mod=source), [])

    def test_a_write_before_the_engine_call_is_reported(self):
        source = (
            "from . import host\ndef bad(gh, item):\n    gh.merge(1, 'a')\n    host.step(item)\n"
        )
        self.assertEqual(found(mod=source), ["mod.bad"])

    def test_a_write_after_an_engine_call_in_one_branch_only_is_reported(self):
        source = (
            "from . import host\n"
            "def bad(gh, item, flag):\n"
            "    if flag:\n"
            "        host.step(item)\n"
            "    gh.merge(1, 'a')\n"
        )
        self.assertEqual(found(mod=source), ["mod.bad"])

    def test_a_write_after_an_if_that_asks_in_every_branch_passes(self):
        source = (
            "from . import host\n"
            "def good(gh, item, flag):\n"
            "    if flag:\n"
            "        host.step(item)\n"
            "    else:\n"
            "        host.step(None)\n"
            "    gh.merge(1, 'a')\n"
        )
        self.assertEqual(found(mod=source), [])

    def test_a_write_after_an_if_that_asks_in_one_branch_and_returns_in_the_other_passes(self):
        source = (
            "from . import host\n"
            "def good(gh, item, flag):\n"
            "    if flag:\n"
            "        host.step(item)\n"
            "    else:\n"
            "        return None\n"
            "    gh.merge(1, 'a')\n"
            "def raises(gh, item, flag):\n"
            "    if flag:\n"
            "        raise ValueError(flag)\n"
            "    elif item:\n"
            "        host.step(item)\n"
            "    else:\n"
            "        host.step(None)\n"
            "    gh.merge(1, 'a')\n"
        )
        self.assertEqual(found(mod=source), [])

    def test_an_if_with_no_else_does_not_count_even_when_its_body_returns(self):
        source = (
            "from . import host\n"
            "def bad(gh, item, flag):\n"
            "    if flag:\n"
            "        host.step(item)\n"
            "        return None\n"
            "    gh.merge(1, 'a')\n"
        )
        self.assertEqual(found(mod=source), ["mod.bad"])

    def test_an_if_whose_other_branch_neither_asks_nor_leaves_is_reported(self):
        source = (
            "from . import host\n"
            "def bad(gh, item, flag):\n"
            "    if flag:\n"
            "        host.step(item)\n"
            "    else:\n"
            "        item = None\n"
            "    gh.merge(1, 'a')\n"
        )
        self.assertEqual(found(mod=source), ["mod.bad"])

    def test_a_loop_does_not_count_as_asking(self):
        source = (
            "from . import host\n"
            "def bad(gh, items):\n"
            "    for item in items:\n"
            "        host.step(item)\n"
            "    gh.merge(1, 'a')\n"
        )
        self.assertEqual(found(mod=source), ["mod.bad"])

    def test_an_engine_call_in_the_try_before_the_write_in_its_else_counts(self):
        source = (
            "from . import host\n"
            "def good(gh, item):\n"
            "    try:\n"
            "        host.step(item)\n"
            "    except ValueError:\n"
            "        return None\n"
            "    else:\n"
            "        gh.merge(1, 'a')\n"
        )
        self.assertEqual(found(mod=source), [])

    def test_an_autopilot_step_that_starts_merges_closes_or_splits_is_reported(self):
        source = (
            "".join(
                f"def {kind}(issue):\n    return _step(issue, '{kind}', 'x')\n"
                for kind in ("start", "merge", "close", "split")
            )
            + "def waits(issue):\n    return _step(issue, 'waits', 'x')\n"
        )
        self.assertEqual(found(mod=source), ["mod.close", "mod.merge", "mod.split", "mod.start"])

    def test_a_step_through_the_module_that_makes_it_is_reported(self):
        modules = {
            "autopilot": "def _step(issue, kind, reason=''):\n    return {'state': kind}\n",
            "mod": (
                "from . import autopilot\n"
                "def bad(issue):\n    return autopilot._step(issue, 'close', 'x')\n"
            ),
        }
        self.assertEqual(found(**modules), ["mod.bad"])


class Helpers(unittest.TestCase):
    def test_a_helper_that_writes_passes_its_write_to_a_caller_that_asked(self):
        modules = {
            "util": "def merge_pull(gh, pr):\n    return gh.merge(pr, 'a')\n",
            "mod": (
                "from . import host\n"
                "from .util import merge_pull\n"
                "def good(gh, item):\n"
                "    host.step(item)\n"
                "    return merge_pull(gh, 1)\n"
            ),
        }
        self.assertEqual(found(**modules), [])

    def test_a_caller_that_did_not_ask_is_reported_and_not_its_helper(self):
        modules = {
            "util": "def merge_pull(gh, pr):\n    return gh.merge(pr, 'a')\n",
            "mod": ("from . import util\ndef bad(gh):\n    return util.merge_pull(gh, 1)\n"),
        }
        self.assertEqual(found(**modules), ["mod.bad"])

    def test_only_the_top_of_a_chain_is_reported(self):
        source = (
            "def low(gh):\n    gh.close_pull(1)\n"
            "def middle(gh):\n    low(gh)\n"
            "def top(gh):\n    middle(gh)\n"
        )
        self.assertEqual(found(mod=source), ["mod.top"])

    def test_a_helper_that_asks_makes_its_caller_answered(self):
        source = (
            "from . import host\n"
            "def ask(item):\n    return host.step(item)\n"
            "def good(gh, item):\n    ask(item)\n    gh.merge(1, 'a')\n"
        )
        self.assertEqual(found(mod=source), [])

    def test_a_helper_called_by_a_function_that_asked_and_one_that_did_not_reports_the_second(self):
        source = (
            "from . import host\n"
            "def low(gh):\n    gh.merge(1, 'a')\n"
            "def good(gh, item):\n    host.step(item)\n    low(gh)\n"
            "def bad(gh):\n    low(gh)\n"
        )
        self.assertEqual(found(mod=source), ["mod.bad"])

    def test_a_helper_that_nothing_calls_is_reported_itself(self):
        source = "def entry(gh):\n    gh.merge(1, 'a')\n"
        self.assertEqual(found(mod=source), ["mod.entry"])

    def test_a_call_of_a_module_function_is_not_a_write_of_the_client(self):
        modules = {
            "maintain": "def update_branch(gh, pr):\n    return None\n",
            "mod": "from . import maintain\ndef good(gh):\n    maintain.update_branch(gh, 1)\n",
        }
        self.assertEqual(found(**modules), [])

    def test_a_name_is_looked_up_in_the_function_that_imports_it(self):
        modules = {
            "a": "def report(gh):\n    gh.merge(1, 'a')\n",
            "b": "def report(gh):\n    return None\n",
            "mod": (
                "def good(gh):\n"
                "    from .b import report\n"
                "    report(gh)\n"
                "def bad(gh):\n"
                "    from .a import report\n"
                "    report(gh)\n"
            ),
        }
        self.assertEqual(found(**modules), ["mod.bad"])

    def test_a_nested_function_is_judged_apart_from_its_outer_function(self):
        source = (
            "from . import host\n"
            "def outer(gh, item):\n"
            "    def inner():\n"
            "        gh.merge(1, 'a')\n"
            "    host.step(item)\n"
            "    inner()\n"
        )
        self.assertEqual(found(mod=source), [])
        source = "def outer(gh):\n    def inner():\n        gh.merge(1, 'a')\n    inner()\n"
        self.assertEqual(found(mod=source), ["mod.outer"])

    def test_an_argument_with_the_name_of_a_function_is_not_that_function(self):
        modules = {
            "util": "def ask(item):\n    return item\n",
            "mod": "from .util import ask\ndef good(gh, ask):\n    ask(gh)\n    gh.merge(1, 'a')\n",
        }
        # `ask` here is the argument, which no one can tell asks: the write is not answered.
        self.assertEqual(found(**modules), ["mod.good"])


class Decisions(unittest.TestCase):
    def test_a_close_answer_decision_with_no_engine_call_before_it_is_reported(self):
        source = "def bad(issue):\n    return {'task': 'close_answer', 'issue': issue}\n"
        self.assertEqual(found(route=source), ["route.bad"])

    def test_an_on_base_decision_with_no_engine_call_before_it_is_reported(self):
        source = "def bad(issue):\n    return {'task': 'on_base', 'issue': issue}\n"
        self.assertEqual(found(route=source), ["route.bad"])

    def test_a_decision_after_an_engine_call_passes(self):
        source = (
            "from . import host\n"
            "def good(issue):\n"
            "    answer = host.step(issue)\n"
            "    return {'task': 'close_answer', 'issue': issue}, {'task': 'on_base'}\n"
        )
        self.assertEqual(found(route=source), [])

    def test_a_decision_after_an_engine_call_in_one_branch_is_reported(self):
        source = (
            "from . import host\n"
            "def bad(issue, flag):\n"
            "    if flag:\n"
            "        host.step(issue)\n"
            "    return {'task': 'on_base'}\n"
        )
        self.assertEqual(found(route=source), ["route.bad"])

    def test_a_decision_is_reported_where_it_is_built_whoever_calls_it(self):
        source = (
            "from . import host\n"
            "def build(issue):\n    return {'task': 'on_base'}\n"
            "def good(issue):\n    host.step(issue)\n    return build(issue)\n"
        )
        self.assertEqual(found(route=source), ["route.build"])

    def test_another_task_is_not_a_decision_that_ends_work(self):
        source = "def good(issue):\n    return {'task': 'sweep'}\n"
        self.assertEqual(found(route=source), [])

    def test_a_decision_of_a_named_function_is_not_reported(self):
        source = "def bad(issue):\n    return {'task': 'on_base'}\n"
        self.assertEqual(
            unanswered_writes({"route": source}, named={"route.bad": "carried out"}), []
        )


if __name__ == "__main__":
    unittest.main()
