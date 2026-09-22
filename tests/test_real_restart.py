"""Real kill-and-restart integration test (one, per Contract §10 step 3).

A child process genuinely executes the EXECUTING commit and then dies via
os._exit (SIGKILL semantics — no cleanup). The parent reopens the database
and proves the durability semantics against the on-disk state.
"""
import os
import subprocess
import sys
import textwrap

import pytest

from v5.store import Store
from v5 import execution, obligations, recovery, work
from v5.enums import ActionStatus, ObligationDisposition, TaskStatus
from v5.models import Ok


CHILD = textwrap.dedent("""
    import os
    import sys
    sys.path.insert(0, {root!r})
    from v5.store import Store
    from v5 import work, execution
    from v5.models import CompletionPolicy, RetryBudget
    from v5.enums import TaskStatus

    store = Store({db!r})
    goal = work.create_goal(store, CompletionPolicy(rule="ALL_REQUIRED"))
    task = work.create_task(store, goal.id, CompletionPolicy(rule="ALL_REQUIRED"),
                            RetryBudget(max_attempts=2)).value
    plan = work.create_plan(store, task.id).value
    step = work.create_step(store, plan.id, required=True).value
    task = work.transition_object(store, task.id, TaskStatus.ACTIVE, task.revision).value
    task = work.activate_plan(store, task.id, plan.id, task.revision).value
    action = execution.create_action(store, step.id, "file_write",
                                     {{"path": {target!r}, "content": "x"}},
                                     __import__("v5.enums", fromlist=["IdempotencyClass"]).IdempotencyClass.IDEMPOTENT).value
    started = execution.begin_executing(store, action.id, action.revision,
                                        expected_plan_id=plan.id).value
    # the EXECUTING commit is durable; now die mid-external-effect with no cleanup
    print(started.id, flush=True)
    os._exit(9)
""")


class TestRealKillRestart:
    def test_kill_after_executing_commit(self, tmp_path):
        db = tmp_path / "state.db"
        target = tmp_path / "child_write.txt"

        # Phase 1: child process creates state and dies hard mid-execution.
        code = CHILD.format(root=os.getcwd(), db=str(db), target=str(target))
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": os.getcwd()},
        )
        # os._exit(9) is an intentional hard death — the returncode proves it
        assert proc.returncode == 9, f"child should have been killed, got {proc.returncode}: {proc.stderr}"
        action_id = proc.stdout.strip().splitlines()[-1]

        # Phase 2: parent reopens the on-disk state as a fresh process.
        store = Store(str(db))
        classification = execution.classify_action_state(store, action_id)
        assert classification == execution.KNOWN_STARTED  # EXECUTING persisted (Law 8)

        out = recovery.recover(store)
        assert action_id in out["recovered_to_unknown"]
        a = execution.load_action(store.read(), action_id)
        assert a.status == ActionStatus.UNKNOWN_OUTCOME       # never assumed unstarted (Law 9)
        assert execution.observation_count(store, action_id) == 0
        assert not target.exists()

        # the obligation is durable and discoverable (Law 17)
        open_obls = obligations.list_open_obligations(store)
        assert len(open_obls) == 1
        assert open_obls[0].origin_action_id == action_id
        assert open_obls[0].disposition == ObligationDisposition.OPEN

        # work objects survived intact
        tasks = store.read().execute("SELECT * FROM tasks").fetchall()
        assert len(tasks) == 1
        assert work.load_task(store.read(), tasks[0]["id"]).status == TaskStatus.ACTIVE
        store.close()
