import asyncio
import datetime
import logging
import uuid
from typing import Any, Dict, Iterable, Optional

from procrastinate import connector, exceptions, jobs, sql, utils

logger = logging.getLogger(__name__)


def get_channel_for_queues(queues: Optional[Iterable[str]] = None) -> Iterable[str]:
    if queues is None:
        return ["procrastinate_any_queue"]
    else:
        return ["procrastinate_queue#" + queue for queue in queues]


class JobManager:
    def __init__(self, connector: connector.BaseConnector):
        self.connector = connector

    async def defer_job_async(self, job: jobs.Job) -> jobs.Job:
        """
        Add a job in its queue for later processing by a worker.

        Parameters
        ----------
        job : `jobs.Job`

        Returns
        -------
        `jobs.Job`
            A copy of the job instance with the id set.
        """
        # Make sure this code stays synchronized with .defer_job()
        try:
            result = await self.connector.execute_query_one_async(
                **self._defer_job_query_kwargs(job=job)
            )
        except exceptions.UniqueViolation as exc:
            self._raise_already_enqueued(exc=exc, queueing_lock=job.queueing_lock)

        return job.evolve(id=result["id"])

    def defer_job(self, job: jobs.Job) -> jobs.Job:
        """
        Sync version of `defer_job_async`.
        """
        try:
            result = self.connector.execute_query_one(
                **self._defer_job_query_kwargs(job=job)
            )
        except exceptions.UniqueViolation as exc:
            self._raise_already_enqueued(exc=exc, queueing_lock=job.queueing_lock)

        return job.evolve(id=result["id"])

    def _defer_job_query_kwargs(self, job: jobs.Job) -> Dict[str, Any]:

        return {
            "query": sql.queries["defer_job"],
            "task_name": job.task_name,
            "queue": job.queue,
            "lock": job.lock or str(uuid.uuid4()),
            "queueing_lock": job.queueing_lock,
            "args": job.task_kwargs,
            "scheduled_at": job.scheduled_at,
        }

    def _raise_already_enqueued(
        self, exc: exceptions.UniqueViolation, queueing_lock: Optional[str]
    ):
        if exc.constraint_name == connector.QUEUEING_LOCK_CONSTRAINT:
            raise exceptions.AlreadyEnqueued(
                "Job cannot be enqueued: there is already a job in the queue "
                f"with the queueing lock {queueing_lock}"
            ) from exc
        raise exc

    async def defer_periodic_job(self, task, defer_timestamp) -> Optional[int]:
        """
        Defer a periodic job, ensuring that no other worker will defer a job for the
        same timestamp.
        """
        try:
            result = await self.connector.execute_query_one_async(
                query=sql.queries["defer_periodic_job"],
                task_name=task.name,
                queue=task.queue,
                lock=task.lock,
                queueing_lock=task.queueing_lock,
                defer_timestamp=defer_timestamp,
            )
        except exceptions.UniqueViolation as exc:
            self._raise_already_enqueued(exc=exc, queueing_lock=task.queueing_lock)

        return result["id"]

    async def fetch_job(self, queues: Optional[Iterable[str]]) -> Optional[jobs.Job]:
        """
        Select a job in the queue, and mark it as doing.
        The worker selecting a job is then responsible for running it, and then
        to update the DB with the new status once it's done.

        Parameters
        ----------
        queues : ``Optional[Iterable[str]]``
            Filter by job queue names

        Returns
        -------
        ``Optional[jobs.Job]``
            None if no suitable job was found. The job otherwise.
        """

        row = await self.connector.execute_query_one_async(
            query=sql.queries["fetch_job"], queues=queues
        )

        # fetch_tasks will always return a row, but is there's no relevant
        # value, it will all be None
        if row["id"] is None:
            return None

        return jobs.Job.from_row(row)

    async def get_stalled_jobs(
        self,
        nb_seconds: int,
        queue: Optional[str] = None,
        task_name: Optional[str] = None,
    ) -> Iterable[jobs.Job]:
        """
        Return all jobs that have been in ``todo`` state for more than a given time.

        Parameters
        ----------
        nb_seconds : ``int``
            Only jobs that have been in ``todo`` state for longer than this will be
            returned
        queue : ``Optional[str]``
            Filter by job queue name
        task_name : ``Optional[str]``
            Filter by job task name

        Returns
        -------
        ``Iterable[jobs.Job]``
        """
        rows = await self.connector.execute_query_all_async(
            query=sql.queries["select_stalled_jobs"],
            nb_seconds=nb_seconds,
            queue=queue,
            task_name=task_name,
        )
        return [jobs.Job.from_row(row) for row in rows]

    async def delete_old_jobs(
        self,
        nb_hours: int,
        queue: Optional[str] = None,
        include_error: Optional[bool] = False,
    ) -> None:
        """
        Delete jobs that have reached a final state (``succeeded`` or ``failed``).

        Parameters
        ----------
        nb_hours : ``int``
            Consider jobs that been in a final state for more than ``nb_hours``
        queue : ``Optional[str]``
            Filter by job queue name
        include_error : ``Optional[bool]``
            If ``True``, only succeeded jobs will be considered. If ``False``, both
            succeeded and failed jobs will be considered, ``False`` by default
        """
        # We only consider finished jobs by default
        if not include_error:
            statuses = [jobs.Status.SUCCEEDED.value]
        else:
            statuses = [jobs.Status.SUCCEEDED.value, jobs.Status.FAILED.value]

        await self.connector.execute_query_async(
            query=sql.queries["delete_old_jobs"],
            nb_hours=nb_hours,
            queue=queue,
            statuses=tuple(statuses),
        )

    async def finish_job(
        self,
        job: jobs.Job,
        status: jobs.Status,
        delete_job: bool,
    ) -> None:
        """
        Set a job to its final state (``succeeded`` or ``failed``).

        Parameters
        ----------
        job : `jobs.Job`
        status : `jobs.Status`
            ``succeeded`` or ``failed``
        """
        assert job.id  # TODO remove this
        await self.finish_job_by_id(job_id=job.id, status=status, delete_job=delete_job)

    async def finish_job_by_id(
        self,
        job_id: int,
        status: jobs.Status,
        delete_job: bool,
    ) -> None:
        await self.connector.execute_query_async(
            query=sql.queries["finish_job"],
            job_id=job_id,
            status=status.value,
            delete_job=delete_job,
        )

    async def retry_job(
        self,
        job: jobs.Job,
        retry_at: datetime.datetime,
    ) -> None:
        """
        Indicates that a job should be retried later.

        Parameters
        ----------
        job : `jobs.Job`
        retry_at : ``datetime.datetime``
            If set at present time or in the past, the job may be retried immediately.
            Otherwise, the job will be retried no sooner than this date & time.
            Should be timezone-aware (even if UTC)
        """
        assert job.id  # TODO remove this
        await self.retry_job_by_id(job_id=job.id, retry_at=retry_at)

    async def retry_job_by_id(
        self,
        job_id: int,
        retry_at: datetime.datetime,
    ) -> None:
        await self.connector.execute_query_async(
            query=sql.queries["retry_job"],
            job_id=job_id,
            retry_at=retry_at,
        )

    async def listen_for_jobs(
        self, *, event: asyncio.Event, queues: Optional[Iterable[str]] = None
    ) -> None:
        """
        Listens to defer operation in the database, and raises the event each time an
        defer operation is seen.

        This coroutine either returns ``None`` upon calling if it cannot start
        listening or does not return and needs to be canceled to end.

        Parameters
        ----------
        event : ``asyncio.Event``
            This event will be set each time a defer operation occurs
        queues : ``Optional[Iterable[str]]``
            If ``None``, all defer operations will be considered. If an iterable of
            queue names is passed, only defer operations on those queues will be
            considered. Defaults to ``None``
        """
        await self.connector.listen_notify(
            event=event, channels=get_channel_for_queues(queues=queues)
        )

    async def check_connection(self) -> bool:
        """
        Dummy query, check that the main Procrastinate SQL table exists.
        Raises if there's a connection problem.

        Returns
        -------
        ``bool``
            ``True`` if the table exists, ``False`` otherwise.
        """
        result = await self.connector.execute_query_one_async(
            query=sql.queries["check_connection"],
        )
        return result["check"] is not None

    async def list_jobs_async(
        self,
        id: Optional[int] = None,
        queue: Optional[str] = None,
        task: Optional[str] = None,
        status: Optional[str] = None,
        lock: Optional[str] = None,
        queueing_lock: Optional[str] = None,
    ) -> Iterable[Dict[str, Any]]:
        """
        List all procrastinate jobs given query filters.

        Parameters
        ----------
        id : ``int``
            Filter by job ID
        queue : ``str``
            Filter by job queue name
        task : ``str``
            Filter by job task name
        status : ``str``
            Filter by job status (``todo``/``doing``/``succeeded``/``failed``)
        lock : ``str``
            Filter by job lock
        queueing_lock : ``str``
            Filter by job queueing_lock

        Returns
        -------
        ``List[Dict[str, Any]]``
            A list of dictionaries representing jobs (``id``, ``queue``, ``task``,
            ``lock``, ``args``, ``status``, ``scheduled_at``, ``attempts``).
        """
        return [
            {
                "id": row["id"],
                "queue": row["queue_name"],
                "task": row["task_name"],
                "lock": row["lock"],
                "queueing_lock": row["queueing_lock"],
                "args": row["args"],
                "status": row["status"],
                "scheduled_at": row["scheduled_at"],
                "attempts": row["attempts"],
            }
            for row in await self.connector.execute_query_all_async(
                query=sql.queries["list_jobs"],
                id=id,
                queue_name=queue,
                task_name=task,
                status=status,
                lock=lock,
                queueing_lock=queueing_lock,
            )
        ]

    async def list_queues_async(
        self,
        queue: Optional[str] = None,
        task: Optional[str] = None,
        status: Optional[str] = None,
        lock: Optional[str] = None,
    ) -> Iterable[Dict[str, Any]]:
        """
        List all queues and number of jobs per status for each queue.

        Parameters
        ----------
        queue : ``str``
            Filter by job queue name
        task : ``str``
            Filter by job task name
        status : ``str``
            Filter by job status (``todo``/``doing``/``succeeded``/``failed``)
        lock : ``str``
            Filter by job lock

        Returns
        -------
        ``List[Dict[str, Any]]``
            A list of dictionaries representing queues stats (``name``, ``jobs_count``,
            ``todo``, ``doing``, ``succeeded``, ``failed``).
        """
        return [
            {
                "name": row["name"],
                "jobs_count": row["jobs_count"],
                "todo": row["stats"].get("todo", 0),
                "doing": row["stats"].get("doing", 0),
                "succeeded": row["stats"].get("succeeded", 0),
                "failed": row["stats"].get("failed", 0),
            }
            for row in await self.connector.execute_query_all_async(
                query=sql.queries["list_queues"],
                queue_name=queue,
                task_name=task,
                status=status,
                lock=lock,
            )
        ]

    async def list_tasks_async(
        self,
        queue: Optional[str] = None,
        task: Optional[str] = None,
        status: Optional[str] = None,
        lock: Optional[str] = None,
    ) -> Iterable[Dict[str, Any]]:
        """
        List all tasks and number of jobs per status for each task.

        Parameters
        ----------
        queue : ``str``
            Filter by job queue name
        task : ``str``
            Filter by job task name
        status : ``str``
            Filter by job status (``todo``/``doing``/``succeeded``/``failed``)
        lock : ``str``
            Filter by job lock

        Returns
        -------
        ``List[Dict[str, Any]]``
            A list of dictionaries representing tasks stats (``name``, ``jobs_count``,
            ``todo``, ``doing``, ``succeeded``, ``failed``).
        """
        return [
            {
                "name": row["name"],
                "jobs_count": row["jobs_count"],
                "todo": row["stats"].get("todo", 0),
                "doing": row["stats"].get("doing", 0),
                "succeeded": row["stats"].get("succeeded", 0),
                "failed": row["stats"].get("failed", 0),
            }
            for row in await self.connector.execute_query_all_async(
                query=sql.queries["list_tasks"],
                queue_name=queue,
                task_name=task,
                status=status,
                lock=lock,
            )
        ]

    async def retry_job_return_info_async(self, job_id: int) -> Dict[str, Any]:
        """
        Retry a job, and return its information.

        Parameters
        ----------
        job_id : ``int``

        Returns
        -------
        ``Dict[str, Any]``
            A dictionary representing the job (``id``, ``queue``, ``task``,
            ``lock``, ``args``, ``status``, ``scheduled_at``, ``attempts``).
        """
        await self.retry_job_by_id(
            job_id=job_id, retry_at=utils.utcnow().replace(microsecond=0)
        )
        (result,) = await self.list_jobs_async(id=job_id)
        return result

    async def cancel_job_return_info_async(self, job_id: int) -> Dict[str, Any]:
        """
        Finish a job, setting its status to ``failed``, and return its information.

        Parameters
        ----------
        job_id : ``int``

        Returns
        -------
        ``Dict[str, Any]``
            A dictionary representing the job (``id``, ``queue``, ``task``,
            ``lock``, ``args``, ``status``, ``scheduled_at``, ``attempts``).
        """
        await self.finish_job_by_id(
            job_id=job_id, status=jobs.Status.FAILED, delete_job=False
        )
        (result,) = await self.list_jobs_async(id=job_id)
        return result


utils.add_method_sync_api(cls=JobManager, method_name="list_jobs_async")
utils.add_method_sync_api(cls=JobManager, method_name="list_queues_async")
utils.add_method_sync_api(cls=JobManager, method_name="list_tasks_async")
utils.add_method_sync_api(cls=JobManager, method_name="retry_job_return_info_async")
utils.add_method_sync_api(cls=JobManager, method_name="cancel_job_return_info_async")
