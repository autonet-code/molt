"""
Service wrapper for Moltbook daemon

Run with: python service.py start
Stop with: Ctrl+C or python service.py stop (writes stop file)
"""

import sys
import os
import signal
import time
import json
from pathlib import Path
from datetime import datetime

SERVICE_DIR = Path(__file__).parent
PID_FILE = SERVICE_DIR / "daemon.pid"
STOP_FILE = SERVICE_DIR / "daemon.stop"
STATUS_FILE = SERVICE_DIR / "daemon.status"


def write_status(status: dict):
    status["updated_at"] = datetime.utcnow().isoformat()
    STATUS_FILE.write_text(json.dumps(status, indent=2))


def start_service():
    """Start the daemon service"""
    from daemon import MoltbookDaemon

    # Check if already running
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        print(f"Warning: PID file exists (pid={pid}). Daemon may already be running.")
        print("Use 'python service.py stop' first, or delete daemon.pid if stale.")
        return

    # Clean up stop file if exists
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    # Write PID
    PID_FILE.write_text(str(os.getpid()))
    print(f"Starting daemon (pid={os.getpid()})...")

    # Create daemon with config
    daemon = MoltbookDaemon(
        poll_interval=300,      # 5 minutes
        auto_execute=False      # Queue for review, don't auto-post
    )

    # Signal handler for graceful shutdown
    def shutdown(signum, frame):
        print("\nShutdown signal received...")
        daemon.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Status update loop wrapper
    def run_with_status():
        daemon.running = True
        write_status({"status": "running", "pid": os.getpid()})

        while daemon.running:
            # Check for stop file
            if STOP_FILE.exists():
                print("Stop file detected, shutting down...")
                daemon.running = False
                break

            try:
                daemon._poll_cycle()
                write_status({
                    "status": "running",
                    "pid": os.getpid(),
                    "last_poll": datetime.utcnow().isoformat(),
                    "queue_size": len(daemon.action_queue),
                    "posts_today": daemon._posts_today,
                    "replies_this_hour": daemon._replies_this_hour
                })
            except Exception as e:
                print(f"Error in poll cycle: {e}")
                write_status({"status": "error", "error": str(e)})

            # Sleep in chunks so we can respond to stop signal
            for _ in range(daemon.poll_interval):
                if not daemon.running or STOP_FILE.exists():
                    break
                time.sleep(1)

        # Cleanup
        write_status({"status": "stopped"})
        if PID_FILE.exists():
            PID_FILE.unlink()
        if STOP_FILE.exists():
            STOP_FILE.unlink()
        print("Daemon stopped.")

    run_with_status()


def stop_service():
    """Signal the daemon to stop"""
    if not PID_FILE.exists():
        print("No PID file found - daemon may not be running")
        return

    pid = int(PID_FILE.read_text().strip())
    print(f"Signaling daemon (pid={pid}) to stop...")

    # Write stop file
    STOP_FILE.write_text("stop")
    print("Stop file written. Daemon will stop after current cycle.")
    print("(Or press Ctrl+C in the daemon terminal)")


def status_service():
    """Show daemon status"""
    if STATUS_FILE.exists():
        status = json.loads(STATUS_FILE.read_text())
        print(json.dumps(status, indent=2))
    else:
        print("No status file found - daemon may not have run yet")

    if PID_FILE.exists():
        print(f"\nPID file: {PID_FILE.read_text().strip()}")


def poll_once():
    """Run a single poll cycle (for testing/manual use)"""
    from daemon import MoltbookDaemon

    daemon = MoltbookDaemon(poll_interval=60, auto_execute=False)
    daemon.poll_once()

    print("\n=== Queue ===")
    for i, action in enumerate(daemon.get_queue()):
        print(f"[{i}] {action.action_type.value}: {action.target_id}")
        print(f"    Context: {json.dumps(action.context)[:100]}...")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python service.py <command>")
        print("Commands:")
        print("  start   - Start the daemon")
        print("  stop    - Signal daemon to stop")
        print("  status  - Show daemon status")
        print("  poll    - Run one poll cycle (manual)")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "start":
        start_service()
    elif cmd == "stop":
        stop_service()
    elif cmd == "status":
        status_service()
    elif cmd == "poll":
        poll_once()
    else:
        print(f"Unknown command: {cmd}")
