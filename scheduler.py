import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(self, agent_factory: Callable):
        self.scheduler = BackgroundScheduler(daemon=True)
        self.agent_factory = agent_factory
        self._job_configs: dict[str, dict] = {}

    def add_cron_job(self, job_id: str, prompt: str, cron_expr: str) -> None:
        trigger = CronTrigger.from_crontab(cron_expr)

        def run_job():
            try:
                agent = self.agent_factory()
                result = agent.run_scheduled(prompt)
                logger.info(f"Scheduled job '{job_id}' completed at {datetime.now()}")
                print(f"\n[Scheduled: {job_id}] {result}")
            except Exception as e:
                logger.error(f"Scheduled job '{job_id}' failed: {e}")

        self.scheduler.add_job(run_job, trigger, id=job_id, replace_existing=True)
        self._job_configs[job_id] = {
            "prompt": prompt,
            "cron": cron_expr,
        }
        logger.info(f"Added scheduled job '{job_id}': cron='{cron_expr}'")

    def remove_job(self, job_id: str) -> bool:
        try:
            self.scheduler.remove_job(job_id)
            self._job_configs.pop(job_id, None)
            return True
        except Exception:
            return False

    def list_jobs(self) -> list:
        jobs = []
        for job in self.scheduler.get_jobs():
            config = self._job_configs.get(job.id, {})
            jobs.append({
                "id": job.id,
                "prompt": config.get("prompt", ""),
                "cron": config.get("cron", ""),
                "next_run": str(job.next_run_time) if job.next_run_time else "N/A",
            })
        return jobs

    def start(self) -> None:
        if self._job_configs:
            self.scheduler.start()
            logger.info("Scheduler started")

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")
