from __future__ import annotations

import argparse
import os
import signal
from pathlib import Path

from shardferry.agent import AgentApp
from shardferry.agent.executor import LocalArtifactResolver, TargetTaskExecutor


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--kill-on-replace", action="store_true")
    args = parser.parse_args()

    if args.kill_on_replace:
        def kill_replace(source, destination):
            del source, destination
            os.kill(os.getpid(), signal.SIGKILL)

        os.replace = kill_replace

    executor = TargetTaskExecutor(
        LocalArtifactResolver(Path(args.artifact_root)),
    )
    app = AgentApp(
        "secret",
        args.state_dir,
        committer=executor.commit,
    )
    app.recover_tasks()


if __name__ == "__main__":
    main()
