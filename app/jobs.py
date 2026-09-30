"""Background jobs with progress, so long downloads/installs never block a request."""
import asyncio
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "queued"  # queued | running | done | error
    progress: float = 0.0
    message: str = ""
    results: list = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def dict(self):
        return {k: getattr(self, k) for k in
                ("id", "kind", "title", "status", "progress", "message", "results", "created")}


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._sem: asyncio.Semaphore | None = None
        self._tasks: set = set()

    def start(self, kind: str, title: str, work, serial: bool = True) -> Job:
        """work: async fn(job). serial=True queues behind other device jobs."""
        job = Job(uuid.uuid4().hex[:10], kind, title)
        self.jobs[job.id] = job
        for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-40]:
            if old.status in ("done", "error"):
                self.jobs.pop(old.id, None)

        async def runner():
            if self._sem is None:
                self._sem = asyncio.Semaphore(1)
            try:
                if serial:
                    async with self._sem:
                        await self._run(job, work)
                else:
                    await self._run(job, work)
            except Exception as e:  # never leave a job stuck
                job.status, job.message = "error", str(e)

        t = asyncio.create_task(runner())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, work):
        job.status = "running"
        try:
            await work(job)
            failed = job.results and not any(r.get("ok") for r in job.results)
            job.status = "error" if failed else "done"
            job.progress = 1.0
        except Exception as e:
            job.status, job.message = "error", str(e)


manager = JobManager()
