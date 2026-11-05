"""The scaffolding every "scan the library, remember what was done" job shares.

Jersey numbers, clothing and videos each look at the photos a job has not covered yet (pix_ai_scans, keyed by a
stamp of the file and a detail of the model or rules used), work through them in batches on the shared queue
(jobs.py), and record each one as done. Around that core they used to repeat the same bookkeeping: the Progress,
the "start while running" rule, the reset that cancels and empties the tables, the coverage count, and the
prologue and epilogue of the run itself. That bookkeeping lives here once; a module keeps only what is its own:
which photos still need doing (`todo`), what to do with them (`work`), and how to count what is covered (`counts`).

The start rule matters more than it looks: the index rebuilds every few minutes and starts every job again
with the whole library. A start whose to-do list is already inside the running job's is ignored, so a long run
(hours of videos) is not restarted at every rebuild; one with something new cancels the run, and the new
submission picks the rest up (what was recorded stays recorded).
"""

from app import config, jobs
from app.data import vdb


class ScanJob:
    """One scan job: `ScanJob(name, todo, work, counts, ...)`, then start(photos), reset() and coverage().

    todo(con, photos) -> list   the photos (index rows) the job still has to do, read on `con` (a vdb reader)
    work(gen, todo)             the run itself, batch by batch, checking progress.cancelled(gen) between batches
                                and recording each batch in the job's tables and pix_ai_scans
    counts(con) -> (done, total) what coverage() reports (total None when every item is wanted)
    available()                 the job can run here (its model or package is installed); checked at start and
                                again at run time, when the model may have been removed since
    tables                      the job's own tables, emptied by reset() together with its pix_ai_scans rows
    **fields                    the job's own progress fields (phase...), cleared again at each run"""

    def __init__(self, name: str, todo, work, counts=None, available=None, tables: tuple = (), **fields):
        self.name = name
        self.progress = jobs.Progress(name, **fields)
        self._fields = fields
        self._todo, self._work, self._counts, self._available = todo, work, counts, available
        self.tables = tuple(tables)
        self._running = {"todo": set()}  # the photos the running job is working through, for start()'s rule

    def todo(self, photos: list) -> list:
        with vdb.reader() as con:
            return self._todo(con, photos)

    def start(self, photos: list):
        """Do what is still missing for these photos (index rows with id, path, video, hash, size, modified)."""
        if not config.ANALYSIS or (self._available and not self._available()):
            return
        if self.progress["running"] and {p["id"] for p in self.todo(photos)} <= self._running["todo"]:
            return  # nothing new: the running job carries on (an index rebuild must not restart it every few minutes)
        gen = self.progress.cancel()
        jobs.submit(self.name, lambda: self._run(photos, gen))

    def _run(self, photos: list, gen: int):
        if self._available and not self._available():
            return
        todo = self.todo(photos)
        self._running["todo"] = {p["id"] for p in todo}  # one assignment: a start() on another thread never sees it half built
        self.progress.update(running=bool(todo), done=0, total=len(todo), **self._fields)
        if not todo:
            return
        try:
            self._work(gen, todo)
        finally:
            self.progress.finish(gen)

    def reset(self):
        """Throw the results away: the running job stops at its next batch, and the next start does it all again."""
        self.progress.cancel()
        self._running["todo"] = set()
        with vdb.writer() as con:
            for t in self.tables:
                con.execute(f"DELETE FROM {t}")
            con.execute("DELETE FROM pix_ai_scans WHERE job = ?", (self.name,))

    def coverage(self) -> tuple:
        """(done, total) for the Maintenance row; (0, 0) when the database cannot be read (not there yet, locked)."""
        try:
            with vdb.reader() as con:
                done, total = self._counts(con)
            return int(done), (int(total) if total is not None else None)
        except Exception:
            return 0, 0
