# WO-11: Background Worker Runner

## Objective

Implement the worker runner that processes the job queue in the background — extraction, summarization, and compaction jobs. This is the glue that makes all background processing actually run.

## Files to create

- `src/callmem/core/workers.py` — Worker runner (polling loop, job dispatch)
- `tests/unit/test_workers.py`

## Files to modify

- `src/callmem/cli.py` — Add `--no-workers` flag to `serve`, add standalone `callmem workers` command
- `src/callmem/mcp/server.py` — Start worker thread alongside MCP server (unless `--no-workers`)

## Constraints

- Worker runs in a background thread (not a separate process)
- Polling interval: configurable (default 5 seconds)
- Job dispatch: check for pending jobs, process one at a time
- Job types map to handlers: `extract_entities` → extraction worker, `generate_summary` → summarizer, `compact` → compaction worker
- Graceful shutdown: finish current job, then stop
- Thread-safe SQLite access (WAL mode + separate connection per thread)
- Log all job processing (start, complete, fail)
- Optional standalone mode: `callmem workers` runs just the worker loop (for running separately from the MCP server)

## Worker runner design

```python
class WorkerRunner:
    def __init__(self, engine: MemoryEngine, ollama: OllamaClient, config: Config):
        self.queue = engine.queue
        self.handlers = {
            "extract_entities": EntityExtractor(engine, ollama),
            "generate_summary": Summarizer(engine, ollama),
            "compact": Compactor(engine, config),
        }
        self.running = False
        self.poll_interval = config.extraction.delay_s  # Reuse config

    def start(self):
        """Start processing loop in a background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        """Signal the loop to stop and wait for current job."""
        self.running = False
        self.thread.join(timeout=30)

    def _run_loop(self):
        while self.running:
            job = self.queue.dequeue_any()
            if job:
                self._process(job)
            else:
                time.sleep(self.poll_interval)

    def _process(self, job):
        handler = self.handlers.get(job.type)
        if not handler:
            self.queue.fail(job.id, f"Unknown job type: {job.type}")
            return
        try:
            handler.process(job)
            self.queue.complete(job.id)
        except Exception as e:
            self.queue.fail(job.id, str(e))
```

## Acceptance criteria

1. Worker runner starts a background thread
2. Worker processes extraction jobs from the queue
3. Worker processes summarization jobs from the queue
4. Worker processes compaction jobs from the queue
5. Worker handles unknown job types gracefully
6. Worker retries failed jobs up to `max_attempts`
7. Worker stops cleanly when `stop()` is called
8. Worker thread doesn't block the main MCP server thread
9. `callmem workers --project .` runs the worker loop standalone
10. `pytest tests/unit/test_workers.py` passes

## Suggested tests

```python
def test_worker_processes_extraction_job(engine, mock_ollama):
    engine.start_session()
    engine.ingest_one("response", "We should use cursor-based pagination")
    # This should have queued an extraction job
    runner = WorkerRunner(engine, mock_ollama, config)
    runner.process_one()  # Process single job synchronously
    entities = engine.get_entities(type="decision")
    assert len(entities) > 0

def test_worker_stops_cleanly(engine, mock_ollama):
    runner = WorkerRunner(engine, mock_ollama, config)
    runner.start()
    time.sleep(0.5)
    runner.stop()
    assert not runner.thread.is_alive()

def test_worker_retries_failed_job(engine, failing_ollama):
    engine.start_session()
    engine.ingest_one("response", "test")
    runner = WorkerRunner(engine, failing_ollama, config)
    runner.process_one()  # Fails
    job = engine.queue.peek("extract_entities")
    assert job.attempts == 1
    assert job.status == "pending"  # Available for retry
```
