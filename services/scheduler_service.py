import os
import atexit
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(daemon=True)

LOCK_FILE = os.path.join(os.path.abspath(os.path.dirname(__file__)), "scheduler.lock")

def run_job_sync(app):
    with app.app_context():
        try:
            from services.job_api import JobSyncService
            logger.info("[SCHEDULER] Background Job Sync triggered...")
            result = JobSyncService.sync()
            logger.info("[SCHEDULER] Background Job Sync finished: %s", result)
        except Exception as e:
            # Use logger.exception() to capture the FULL traceback in logs, not just the message
            logger.exception("[SCHEDULER] Error in background Job Sync: %s", e)

def run_internship_sync(app):
    with app.app_context():
        try:
            from services.job_api import InternshipSyncService
            logger.info("[SCHEDULER] Background Internship Sync triggered...")
            result = InternshipSyncService.sync()
            logger.info("[SCHEDULER] Background Internship Sync finished: %s", result)
        except Exception as e:
            # Use logger.exception() to capture the FULL traceback in logs, not just the message
            logger.exception("[SCHEDULER] Error in background Internship Sync: %s", e)

def init_scheduler(app):
    """
    Initialize and start the background scheduler.
    Uses a cross-platform lock file to ensure that only ONE Gunicorn worker
    process starts the scheduler, avoiding duplicate cron tasks.
    """
    # Detect Gunicorn / production environment.
    # Render does not set SERVER_SOFTWARE, so also check FLASK_ENV and RENDER env vars.
    is_gunicorn = (
        "gunicorn" in os.environ.get("SERVER_SOFTWARE", "")
        or os.environ.get("FLASK_ENV") == "production"
        or os.environ.get("RENDER") == "true"
    )
    
    if is_gunicorn:
        # Check lock file
        if os.path.exists(LOCK_FILE):
            try:
                # Try to remove the file to check if it's stale (e.g. from an old crashed run)
                # If we can remove it, that means no current process is locking it.
                # However, under high concurrency, we want to try creating it.
                os.remove(LOCK_FILE)
            except OSError:
                # File is currently locked / in use by another worker process
                logger.info("Scheduler lock is held by another process. Skipping initialization in this worker.")
                return scheduler

        # Try to acquire lock by creating the file
        try:
            with open(LOCK_FILE, "w") as f:
                f.write(str(os.getpid()))
            # Ensure lock file is cleaned up at exit
            atexit.register(cleanup_lock)
        except IOError:
            logger.info("Failed to acquire scheduler lock. Skipping scheduler startup in this process.")
            return scheduler

    if scheduler.running:
        return scheduler

    # Register cron jobs:
    # Jobs: every 1 hour (0 * * * *)
    scheduler.add_job(
        func=run_job_sync,
        trigger=CronTrigger.from_crontab('0 * * * *'),
        id='job_sync',
        args=[app],
        replace_existing=True
    )
    
    # Internships: every 2 hours (0 */2 * * *)
    scheduler.add_job(
        func=run_internship_sync,
        trigger=CronTrigger.from_crontab('0 */2 * * *'),
        id='internship_sync',
        args=[app],
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("[SCHEDULER] Scheduler successfully started in background process (PID: %s).", os.getpid())
    logger.info("[SCHEDULER] Jobs scheduled: job_sync (hourly), internship_sync (every 2 hours).")
    return scheduler

def cleanup_lock():
    """Remove the lock file on normal process termination."""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            logger.info("Scheduler lockfile successfully cleaned up.")
    except Exception as e:
        logger.warning(f"Could not clean up scheduler lock file: {e}")

# Register standard cleanup handler
atexit.register(cleanup_lock)
