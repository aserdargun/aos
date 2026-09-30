import asyncio
from pathlib import Path

from .contracts import REPO_ROOT, digest
from .reusable_decider import ReusableDeciderEngine


class LayaCandidateEngine(ReusableDeciderEngine):
    def __init__(self, manifest: Path, python: Path, timeout: float = 120):
        super().__init__(manifest, python, timeout=timeout, cpu_prewarm=False)
        self.identity = {"deployment_id": "laya-candidate-" + digest(self.pins),
                         "kind": "laya_candidate", "real_model": True, "pins": self.pins}

    async def start(self):
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            str(self.python), str(REPO_ROOT / "services/laya/worker.py"), str(self.manifest),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                 "TOKENIZERS_PARALLELISM": "false", "USE_TF": "0", "PYTHONDONTWRITEBYTECODE": "1"},
            start_new_session=True, limit=65536))
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.process = await spawning
            await self.stop_process()
            raise
