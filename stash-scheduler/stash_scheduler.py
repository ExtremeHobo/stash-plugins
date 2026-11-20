#!/usr/bin/env python3
"""
stash_scheduler.py — Stash Scheduler Plugin

All plugin tasks (invoked by Stash via stdin JSON) exit IMMEDIATELY so they
never occupy the Stash job queue. Long-running work is handed off to detached
background subprocesses that run outside Stash's queue entirely.

Plugin task modes (mode comes from defaultArgs in the manifest):
  start_scheduler  — Save config, kill any existing daemon, start daemon in
                     background, return immediately.
  run_now          — Fire scan mutation, return immediately. If "Run Identify
                     After Scan" is enabled, launch a background subprocess to
                     wait for the scan and then trigger identify.
  force_now        — Fire scan mutation and ALWAYS launch a background subprocess
                     for identify afterwards, regardless of settings.
  check_status     — Report whether the daemon is running and show recent log
                     lines. Safe to run at any time; never modifies state.

Background subprocess modes (invoked via sys.argv, never by Stash directly):
  --daemon                          — Run the APScheduler loop.
  --after-identify <job_id> <mins>  — Wait for scan job to finish, then
                                      trigger identify.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Paths (all in temp dir so subprocesses find them regardless of cwd)
# ---------------------------------------------------------------------------
_TMP = tempfile.gettempdir()
CONFIG_FILE = os.path.join(_TMP, "stash-scheduler-config.json")
PID_FILE    = os.path.join(_TMP, "stash-scheduler-daemon.pid")
LOG_FILE    = os.path.join(_TMP, "stash-scheduler-daemon.log")

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------
SCAN_MUTATION = """
mutation MetadataScan($input: ScanMetadataInput!) {
  metadataScan(input: $input)
}
"""

IDENTIFY_MUTATION = """
mutation MetadataIdentify($input: IdentifyMetadataInput!) {
  metadataIdentify(input: $input)
}
"""

JOB_QUEUE_QUERY = """
query JobQueue {
  jobQueue {
    id
    status
    description
    progress
  }
}
"""

FIND_JOB_QUERY = """
query FindJob($id: ID!) {
  findJob(input: { id: $id }) {
    id
    status
    description
    progress
    error
  }
}
"""

# Stash core job description stems that mean "don't start another scan yet".
# Keep these tight: the substring "scan" also matches plugin task names like
# "Running plugin task: Force Scan + Identify Now", which falsely blocked Run Now.
_BUSY_JOB_STEMS = (
    "scanning",
    "identifying",
    "generating",
    "auto tagging",
    "auto-tagging",
    "autotagging",
    "cleaning",
    "optimizing",
    "exporting",
    "importing",
    "migrating",
)

# Stash queues this companion job alongside metadataScan (even when no
# scanGenerate* flags are set). Identify should wait for it too.
_GENERATE_AFTER_SCAN_DESC = "generating for scanned files"

CONFIGURATION_QUERY = """
query Configuration {
  configuration {
    plugins
    defaults {
      identify {
        sources {
          source {
            stash_box_endpoint
            scraper_id
          }
          options {
            fieldOptions {
              field
              strategy
              createMissing
            }
            setCoverImage
            setOrganized
            includeMalePerformers
          }
        }
        options {
          fieldOptions {
            field
            strategy
            createMissing
          }
          setCoverImage
          setOrganized
          includeMalePerformers
        }
      }
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------

def save_config(server_connection, settings):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"server_connection": server_connection, "settings": settings}, f)


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Stash connection
# ---------------------------------------------------------------------------

def _read_api_key_from_stash_config(server_connection):
    """
    Best-effort read of Stash's api_key from config.yml next to the data dir.
    Detached daemon / --after-identify children need this after Stash restarts
    invalidate the SessionCookie captured at Start Scheduler time.
    """
    candidates = []
    data_dir = (server_connection or {}).get("Dir") or ""
    if data_dir:
        candidates.append(os.path.join(data_dir, "config.yml"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".stash", "config.yml"))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("api_key:"):
                        return line.split(":", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def enrich_server_connection(server_connection):
    """
    Return a copy of server_connection with ApiKey filled in when possible.
    Prefer an existing ApiKey; otherwise read config.yml.
    """
    conn = dict(server_connection or {})
    if not conn.get("ApiKey"):
        api_key = _read_api_key_from_stash_config(conn)
        if api_key:
            conn["ApiKey"] = api_key
    return conn


def make_stash(server_connection):
    from stashapi.stashapp import StashInterface
    conn = enrich_server_connection(server_connection)
    # force_api_key swaps the session cookie for a durable ApiKey so the
    # daemon and --after-identify children survive Stash restarts.
    try:
        return StashInterface(conn, force_api_key=True)
    except TypeError:
        # Older stashapp-tools without force_api_key
        return StashInterface(conn)


def call_gql(stash, query, variables=None):
    return stash.call_GQL(query, variables or {})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_plugin_settings(stash, plugin_id="stash-scheduler"):
    defaults = {
        "frequency": "daily",
        "time_of_day": "02:00",
        "day_of_week": "sun",
        "timezone": "UTC",
        "run_identify": False,
        "identify_timeout_minutes": 120,
        # Comma- or newline-separated list of paths to restrict scan + identify.
        # Empty string = full library (default).
        "scanPaths": "",
        # Scan generation flags — all off by default (non-breaking)
        "scanGenerateCovers": False,
        "scanGeneratePreviews": False,
        "scanGenerateImagePreviews": False,
        "scanGenerateSprites": False,
        "scanGeneratePhashes": False,
        "scanGenerateImagePhashes": False,
        "scanGenerateThumbnails": False,
        "scanGenerateClipPreviews": False,
        "rescan": False,
    }
    try:
        result = call_gql(stash, CONFIGURATION_QUERY)
        plugins_cfg = result.get("configuration", {}).get("plugins", {})
        saved = plugins_cfg.get(plugin_id, {})
        defaults.update(saved)
    except Exception as exc:
        stash.log.warning(f"[Stash Scheduler] Could not read plugin settings: {exc}")
    return defaults


def _parse_time_of_day(raw, warn):
    raw = str(raw).strip()
    try:
        parts = raw.split(":")
        if len(parts) != 2:
            raise ValueError("expected HH:MM")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"values out of range: {hh}:{mm:02d}")
        return hh, mm
    except (ValueError, TypeError) as exc:
        warn(f"[Stash Scheduler] Invalid time_of_day {raw!r} ({exc}) — defaulting to 02:00.")
        return 2, 0


def _is_cron_expr(value):
    """Return True if value looks like a 5-field crontab expression."""
    import re as _re
    parts = _re.split(r'\s+', str(value).strip())
    return len(parts) == 5 and any(c in value for c in (' ', '\t'))


def validate_and_coerce_settings(settings, warn):
    VALID_FREQUENCIES = {"hourly", "daily", "weekly"}
    VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

    raw_freq = str(settings.get("frequency", "daily")).strip()

    if _is_cron_expr(raw_freq):
        # Validate the cron expression via APScheduler if available; otherwise
        # trust the structural check and let the daemon catch any errors at start.
        cron_valid = True
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(raw_freq)
        except ImportError:
            pass  # APScheduler not installed yet; daemon will catch invalid exprs at start
        except Exception as exc:
            cron_valid = False
            warn(
                f"[Stash Scheduler] Invalid cron expression {raw_freq!r} ({exc})"
                " — defaulting to 'daily'."
            )

        if cron_valid:
            settings["frequency"] = "cron"
            settings["cron_expr"] = raw_freq
        else:
            raw_freq = "daily"

    if settings.get("frequency") not in ("cron",):
        freq = raw_freq.lower()
        if freq not in VALID_FREQUENCIES:
            warn(f"[Stash Scheduler] Invalid frequency {freq!r} — defaulting to 'daily'.")
            freq = "daily"
        settings["frequency"] = freq

    raw_time = settings.get("time_of_day", "02:00")
    hour, minute = _parse_time_of_day(raw_time, warn)
    settings["time_of_day"] = f"{hour:02d}:{minute:02d}"
    settings["hour"] = hour
    settings["minute"] = minute

    dow = str(settings.get("day_of_week", "sun")).strip().lower()
    if dow not in VALID_DAYS:
        warn(f"[Stash Scheduler] Invalid day_of_week {dow!r} — defaulting to 'sun'.")
        dow = "sun"
    settings["day_of_week"] = dow

    try:
        timeout = int(settings.get("identify_timeout_minutes", 120))
        if timeout < 1:
            raise ValueError(f"must be >= 1, got {timeout}")
    except (ValueError, TypeError) as exc:
        warn(f"[Stash Scheduler] Invalid identify_timeout_minutes ({exc}) — defaulting to 120.")
        timeout = 120
    settings["identify_timeout_minutes"] = timeout

    def _as_bool(value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
        return default

    settings["run_identify"] = _as_bool(settings.get("run_identify", False), False)

    tz_raw = str(settings.get("timezone", "")).strip()
    if not tz_raw:
        tz_raw = "UTC"
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(tz_raw)
    except Exception:
        try:
            from backports import zoneinfo as _bz
            _bz.ZoneInfo(tz_raw)
        except Exception:
            warn(
                f"[Stash Scheduler] Unrecognised timezone {tz_raw!r} — defaulting to UTC."
            )
            tz_raw = "UTC"
    settings["timezone"] = tz_raw

    # scanPaths — parse comma/newline-separated string into a clean list
    raw_paths = str(settings.get("scanPaths", "") or "")
    import re as _re
    scan_paths = [p.strip() for p in _re.split(r"[,\n]+", raw_paths) if p.strip()]
    settings["scan_paths"] = scan_paths  # normalised list; raw "scanPaths" kept for saving

    # Scan generation flags — coerce to bool (string "false" must stay False)
    for flag in (
        "scanGenerateCovers", "scanGeneratePreviews", "scanGenerateImagePreviews",
        "scanGenerateSprites", "scanGeneratePhashes", "scanGenerateImagePhashes",
        "scanGenerateThumbnails", "scanGenerateClipPreviews", "rescan",
    ):
        settings[flag] = _as_bool(settings.get(flag, False), False)

    return settings


# ---------------------------------------------------------------------------
# Scan / Identify helpers (used by both plugin tasks and the daemon)
# ---------------------------------------------------------------------------

_SCAN_FLAGS = (
    "scanGenerateCovers",
    "scanGeneratePreviews",
    "scanGenerateImagePreviews",
    "scanGenerateSprites",
    "scanGeneratePhashes",
    "scanGenerateImagePhashes",
    "scanGenerateThumbnails",
    "scanGenerateClipPreviews",
    "rescan",
)


def trigger_scan(stash_or_log, gql_fn, settings=None):
    """
    Trigger a library scan and return the job ID.
    stash_or_log: a StashInterface, a logging.Logger, or None.
    gql_fn:       callable(query, variables) -> result dict.
    settings:     validated settings dict (used to set scanGenerate* flags).
    """
    scan_input = {}
    if settings:
        for flag in _SCAN_FLAGS:
            if settings.get(flag):
                scan_input[flag] = True
        paths = settings.get("scan_paths") or []
        if paths:
            scan_input["paths"] = paths

    active_flags = [f for f in _SCAN_FLAGS if scan_input.get(f)]
    paths_desc = (
        f" paths=[{', '.join(scan_input['paths'])}]" if scan_input.get("paths") else ""
    )
    if active_flags:
        _log_info(
            stash_or_log,
            "[Stash Scheduler] Triggering library scan with flags: "
            + ", ".join(active_flags) + paths_desc,
        )
    else:
        _log_info(
            stash_or_log,
            "[Stash Scheduler] Triggering library scan"
            + (paths_desc if paths_desc else " (full library)") + "…",
        )

    result = gql_fn(SCAN_MUTATION, {"input": scan_input})
    job_id = result.get("metadataScan")
    if job_id:
        _log_info(stash_or_log, f"[Stash Scheduler] Scan job started (id={job_id})")
    else:
        _log_warn(stash_or_log, "[Stash Scheduler] metadataScan returned no job ID.")
    return job_id


def trigger_identify(stash_or_log, gql_fn, paths=None):
    """
    Trigger an identify task and return the job ID.
    paths: optional list of path strings — when set, only scenes within those
           paths are identified (mirrors the scan path restriction).
    """
    try:
        result = gql_fn(CONFIGURATION_QUERY, {})
        identify = result.get("configuration", {}).get("defaults", {}).get("identify", {})
    except Exception as exc:
        _log_warn(stash_or_log, f"[Stash Scheduler] Could not read identify defaults: {exc}")
        return None

    if not identify or not identify.get("sources"):
        _log_warn(
            stash_or_log,
            "[Stash Scheduler] Identify skipped — no sources configured in "
            "Settings → Identify.",
        )
        return None

    paths_desc = f" (paths: {', '.join(paths)})" if paths else " (full library)"
    _log_info(stash_or_log, f"[Stash Scheduler] Triggering identify task{paths_desc}…")
    try:
        identify_input = {"sources": identify["sources"]}
        if identify.get("options"):
            identify_input["options"] = identify["options"]
        if paths:
            identify_input["paths"] = paths
        result = gql_fn(IDENTIFY_MUTATION, {"input": identify_input})
        job_id = result.get("metadataIdentify")
        _log_info(stash_or_log, f"[Stash Scheduler] Identify job started (id={job_id})")
        return job_id
    except Exception as exc:
        _log_err(stash_or_log, f"[Stash Scheduler] Failed to start identify: {exc}")
        return None


def _lookup_job(gql_fn, job_id):
    """
    Resolve a job by ID. Prefer findJob (works after the job leaves the live
    queue); fall back to scanning jobQueue for older Stash builds.
    """
    try:
        result = gql_fn(FIND_JOB_QUERY, {"id": str(job_id)})
        job = result.get("findJob")
        if job is not None:
            return job
    except Exception:
        pass

    result = gql_fn(JOB_QUEUE_QUERY, {})
    queue = result.get("jobQueue") or []
    return next((j for j in queue if str(j.get("id")) == str(job_id)), None)


def _is_blocking_library_job(description):
    """
    True for active Stash library jobs (Scanning… / Identifying… / …).
    False for plugin tasks — including this plugin's own Run/Force Scan tasks,
    whose titles contain the word "scan" and must not block themselves.
    """
    desc = str(description or "").strip().lower()
    if not desc:
        return False
    if desc.startswith("running plugin task"):
        return False
    return any(stem in desc for stem in _BUSY_JOB_STEMS)


def library_jobs_busy(gql_fn, stash_or_log=None):
    """
    Return True if Stash already has an active library job that should block a
    new scheduled scan (scan / identify / generate / etc.).
    """
    try:
        result = gql_fn(JOB_QUEUE_QUERY, {})
        queue = result.get("jobQueue") or []
    except Exception as exc:
        _log_warn(
            stash_or_log,
            f"[Stash Scheduler] Could not read job queue (treating as busy): {exc}",
        )
        return True

    blockers = []
    for job in queue:
        status = str(job.get("status") or "").upper()
        if status in ("FINISHED", "CANCELLED", "FAILED"):
            continue
        desc = str(job.get("description") or "")
        if _is_blocking_library_job(desc):
            blockers.append(f"{desc} (id={job.get('id')}, {status})")

    if blockers:
        _log_warn(
            stash_or_log,
            "[Stash Scheduler] Active library job(s) in queue: " + "; ".join(blockers),
        )
        return True
    return False


def _active_generate_after_scan_ids(gql_fn):
    """Return job IDs for active 'Generating for scanned files...' queue entries."""
    try:
        result = gql_fn(JOB_QUEUE_QUERY, {})
        queue = result.get("jobQueue") or []
    except Exception:
        return set()
    ids = set()
    for job in queue:
        status = str(job.get("status") or "").upper()
        if status in ("FINISHED", "CANCELLED", "FAILED"):
            continue
        desc = str(job.get("description") or "").strip().lower()
        if _GENERATE_AFTER_SCAN_DESC in desc:
            ids.add(str(job.get("id")))
    return ids


def _wait_for_jobs(stash_or_log, gql_fn, job_ids, deadline, label):
    """
    Poll until every job_id is finished/cancelled/failed/missing (seen once).
    Returns True if all completed before deadline, False on timeout.
    """
    pending = {str(j) for j in job_ids if j}
    if not pending:
        return True

    seen = set()
    consecutive_missing = {jid: 0 for jid in pending}

    while pending and time.time() < deadline:
        for jid in list(pending):
            try:
                job = _lookup_job(gql_fn, jid)
            except Exception as exc:
                _log_warn(
                    stash_or_log,
                    f"[Stash Scheduler] Error polling {label} job {jid}: {exc}",
                )
                time.sleep(5)
                continue

            if job is not None:
                seen.add(jid)
                consecutive_missing[jid] = 0
                status = str(job.get("status") or "UNKNOWN").upper()
                if status in ("FINISHED", "CANCELLED", "FAILED"):
                    pending.discard(jid)
            elif jid in seen:
                consecutive_missing[jid] = consecutive_missing.get(jid, 0) + 1
                if consecutive_missing[jid] >= 2:
                    pending.discard(jid)
        if pending:
            time.sleep(15)

    if pending:
        _log_warn(
            stash_or_log,
            f"[Stash Scheduler] Timed out waiting for {label} job(s): "
            + ", ".join(sorted(pending)),
        )
        return False
    return True


def wait_for_scan_and_identify(stash_or_log, gql_fn, job_id, timeout_minutes, paths=None):
    """
    Poll until the scan job (and any companion generate-after-scan job) finish,
    then trigger identify.
    Runs in a daemon thread or a detached subprocess — never in a Stash task.
    paths: optional list of path strings passed through to trigger_identify.

    Windows note: jobQueue alone is racey (a just-submitted job can be missing
    for a poll or two, and finished jobs disappear). We require the job to be
    observed at least once, prefer findJob for terminal status, and only treat
    "missing" as complete after repeated misses.

    Stash often queues "Generating for scanned files..." alongside the scan
    (including when no scanGenerate* flags are set). Starting Identify before
    that companion finishes races the queue and left the previous daemon stuck
    reporting busy on every later cron tick.
    """
    if job_id is None:
        _log_warn(stash_or_log, "[Stash Scheduler] No scan job ID — identify skipped.")
        return

    try:
        timeout_minutes = int(timeout_minutes)
    except (TypeError, ValueError):
        timeout_minutes = 120
    if timeout_minutes < 1:
        timeout_minutes = 120

    deadline = time.time() + timeout_minutes * 60
    _log_info(
        stash_or_log,
        f"[Stash Scheduler] Waiting for scan {job_id} to finish "
        f"(timeout {timeout_minutes} min)…",
    )

    seen = False
    consecutive_missing = 0
    generate_ids = set()
    scan_done = False

    while time.time() < deadline:
        try:
            # Track companion generate jobs as soon as Stash queues them.
            generate_ids |= _active_generate_after_scan_ids(gql_fn)

            job = _lookup_job(gql_fn, job_id)

            if job is not None:
                seen = True
                consecutive_missing = 0
                status = str(job.get("status") or "UNKNOWN").upper()
                if status == "FINISHED":
                    scan_done = True
                    break
                if status in ("CANCELLED", "FAILED"):
                    _log_warn(
                        stash_or_log,
                        f"[Stash Scheduler] Scan ended with status {status}. "
                        "Identify skipped.",
                    )
                    return
            elif seen:
                consecutive_missing += 1
                # Require a couple of misses so a transient gap does not
                # treat the scan as done while it is still starting/running.
                if consecutive_missing >= 2:
                    scan_done = True
                    break
            else:
                _log_info(
                    stash_or_log,
                    f"[Stash Scheduler] Scan job {job_id} not visible yet; waiting…",
                )

        except Exception as exc:
            _log_warn(stash_or_log, f"[Stash Scheduler] Error polling job status: {exc}")

        time.sleep(15)

    if not scan_done:
        _log_warn(
            stash_or_log,
            f"[Stash Scheduler] Timed out after {timeout_minutes} min. Identify skipped.",
        )
        return

    _log_info(stash_or_log, "[Stash Scheduler] Scan finished.")

    # Short settle so a generate companion enqueued at scan-end is observed.
    settle_deadline = min(deadline, time.time() + 45)
    while time.time() < settle_deadline:
        generate_ids |= _active_generate_after_scan_ids(gql_fn)
        if generate_ids:
            break
        time.sleep(5)
    # One more refresh in case generate appeared on the last tick.
    generate_ids |= _active_generate_after_scan_ids(gql_fn)

    if generate_ids:
        _log_info(
            stash_or_log,
            "[Stash Scheduler] Waiting for generate-after-scan job(s) "
            + ", ".join(sorted(generate_ids))
            + " before identify…",
        )
        if _wait_for_jobs(
            stash_or_log, gql_fn, generate_ids, deadline, "generate-after-scan"
        ):
            _log_info(stash_or_log, "[Stash Scheduler] Generate-after-scan finished.")
        else:
            # Large libraries with scanGeneratePreviews can outlast the timeout.
            # Still queue Identify rather than skipping it entirely.
            _log_warn(
                stash_or_log,
                "[Stash Scheduler] Generate-after-scan still running after "
                f"{timeout_minutes} min — starting identify anyway.",
            )

    _log_info(stash_or_log, "[Stash Scheduler] Starting identify.")
    trigger_identify(stash_or_log, gql_fn, paths)


# ---------------------------------------------------------------------------
# Logging helpers
# stash_or_log is either a StashInterface (plugin tasks) or a logging.Logger
# (daemon / background subprocesses). None = silent.
# ---------------------------------------------------------------------------

def _log_info(obj, msg):
    if obj is None:
        return
    if isinstance(obj, logging.Logger):
        obj.info(msg)
    else:
        try:
            obj.log.info(msg)
        except Exception:
            pass


def _log_warn(obj, msg):
    if obj is None:
        return
    if isinstance(obj, logging.Logger):
        obj.warning(msg)
    else:
        try:
            obj.log.warning(msg)
        except Exception:
            pass


def _log_err(obj, msg):
    if obj is None:
        return
    if isinstance(obj, logging.Logger):
        obj.error(msg)
    else:
        try:
            obj.log.error(msg)
        except Exception:
            pass


def _make_file_logger(name="stash-scheduler"):
    """Return a Logger that writes to LOG_FILE with timestamps."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    # Also mirror to stderr so it shows up when running interactively
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# Subprocess / daemon helpers
# ---------------------------------------------------------------------------

def _self():
    return os.path.abspath(__file__)


def _windows_clear_stdio_inherit():
    """
    Mark this process's stdin/stdout/stderr as non-inheritable.

    Critical on Windows when Stash launches us with pipes: if a detached
    daemon child inherits those pipe handles, Stash never sees EOF on the
    plugin task stdout and "Start Scheduler" stays Running forever — even
    after the plugin process itself has exited.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        import msvcrt

        HANDLE_FLAG_INHERIT = 0x00000001
        kernel32 = ctypes.windll.kernel32
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            try:
                fd = stream.fileno()
            except Exception:
                continue
            try:
                handle = msvcrt.get_osfhandle(fd)
            except (OSError, ValueError):
                continue
            kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0)
    except Exception:
        pass


def launch_detached(*args):
    """
    Launch this script with the given argv in a fully detached process.

    The child opens LOG_FILE itself via _make_file_logger — do NOT pass an
    open log file handle from the parent. On Windows, redirecting to a parent
    file object forces close_fds=False, which lets the child inherit Stash's
    stdout pipe and hangs the plugin task in the Stash queue forever.
    """
    popen_kwargs = {
        "args": [sys.executable, _self()] + list(args),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }

    if IS_WINDOWS:
        _windows_clear_stdio_inherit()
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        create_new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        detached_process = 0x00000008
        popen_kwargs["creationflags"] = (
            create_no_window | create_new_group | detached_process
        )
    else:
        popen_kwargs["start_new_session"] = True

    subprocess.Popen(**popen_kwargs)


def _pid_exists(pid):
    """Return True if a process with the given PID appears to be alive."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            # Fall through to os.kill(pid, 0) which works on modern CPython Windows
            pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def _force_kill_pid(pid):
    """Best-effort hard kill that works on Windows and Unix."""
    if IS_WINDOWS:
        try:
            # /T kills child processes too (e.g. leftover after-identify helpers)
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            return
        except Exception:
            try:
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                    ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass
            return

    if hasattr(signal, "SIGKILL"):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def kill_existing_daemon():
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return

    try:
        if _pid_exists(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, AttributeError, SystemError):
                pass
            # Give a graceful stop a moment, then hard-kill (required on Windows
            # where SIGTERM is often ignored by non-console Python processes).
            for _ in range(10):
                time.sleep(0.2)
                if not _pid_exists(pid):
                    break
            else:
                _force_kill_pid(pid)
                time.sleep(0.3)
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


def write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def daemon_alive():
    """Return (alive: bool, pid: int|None)."""
    if not os.path.exists(PID_FILE):
        return False, None
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False, None
    if _pid_exists(pid):
        return True, pid
    return False, None


def tail_log(n=30):
    """Return the last n lines of the daemon log file as a string."""
    if not os.path.exists(LOG_FILE):
        return "(log file not found)"
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:]).strip()
    except Exception as exc:
        return f"(could not read log: {exc})"


# ---------------------------------------------------------------------------
# Background mode: --daemon
# ---------------------------------------------------------------------------

def run_daemon():
    log = _make_file_logger()
    log.info("=== Stash Scheduler daemon starting ===")

    try:
        cfg = load_config()
    except Exception as exc:
        log.error(f"Cannot read config file: {exc}")
        sys.exit(1)

    write_pid()
    log.info(f"PID {os.getpid()} written to {PID_FILE}")

    try:
        stash = make_stash(cfg["server_connection"])
    except Exception as exc:
        log.error(f"Cannot connect to Stash: {exc}")
        sys.exit(1)

    settings = validate_and_coerce_settings(cfg["settings"], lambda m: log.warning(m))

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        log.error("APScheduler not installed. Run: pip install apscheduler")
        sys.exit(1)

    frequency = settings["frequency"]
    hour = settings["hour"]
    minute = settings["minute"]
    day_of_week = settings["day_of_week"]
    timezone = settings["timezone"]
    run_identify = settings["run_identify"]
    identify_timeout = settings["identify_timeout_minutes"]
    scan_paths = settings.get("scan_paths") or []

    # Build a simple GQL callable for the daemon (not stash.log-based)
    def gql(query, variables=None):
        return call_gql(stash, query, variables)

    def scheduled_job():
        """
        One scheduled cycle. Runs with max_instances=1 so overlapping cron
        fires cannot stack. Skips when Stash already has library jobs active
        (the main cause of process pile-ups when scan+generate+identify
        outlast the schedule interval).
        """
        log.info("Scheduled scan cycle firing.")

        if library_jobs_busy(gql, log):
            log.warning(
                "Skipping scheduled scan — Stash already has active library "
                "jobs (scan/identify/generate/etc.)."
            )
            return

        try:
            job_id = trigger_scan(log, gql, settings)
        except Exception as exc:
            log.error(f"Scan failed: {exc}")
            return

        if not run_identify:
            return

        # Wait in this scheduler job thread (not a fire-and-forget thread) so
        # max_instances=1 actually prevents overlapping scan+identify cycles.
        # findJob-based polling is Windows-safe; do not use a blind sleep.
        wait_for_scan_and_identify(
            log, gql, job_id, identify_timeout, scan_paths or None
        )

    scheduler = BackgroundScheduler(timezone=timezone)
    job_kwargs = {
        "func": scheduled_job,
        "misfire_grace_time": 3600,
        "coalesce": True,
        "max_instances": 1,
    }

    if frequency == "hourly":
        scheduler.add_job(trigger="cron", minute=0, **job_kwargs)
        log.info(f"Schedule: every hour at :00 ({timezone})")
    elif frequency == "cron":
        from apscheduler.triggers.cron import CronTrigger
        cron_expr = settings["cron_expr"]
        try:
            cron_trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)
        except Exception as exc:
            log.error(f"Invalid cron expression {cron_expr!r}: {exc}. Daemon exiting.")
            sys.exit(1)
        scheduler.add_job(trigger=cron_trigger, **job_kwargs)
        log.info(f"Schedule: crontab '{cron_expr}' ({timezone})")
    elif frequency == "weekly":
        scheduler.add_job(
            trigger="cron", day_of_week=day_of_week, hour=hour, minute=minute, **job_kwargs
        )
        log.info(f"Schedule: weekly {day_of_week.upper()} at {hour:02d}:{minute:02d} ({timezone})")
    else:
        scheduler.add_job(trigger="cron", hour=hour, minute=minute, **job_kwargs)
        log.info(f"Schedule: daily at {hour:02d}:{minute:02d} ({timezone})")

    log.info(f"Identify after scan: {'yes' if run_identify else 'no'}")

    if scan_paths:
        log.info("Scan/identify paths: " + ", ".join(scan_paths))
    else:
        log.info("Scan/identify paths: full library")

    active_flags = [f for f in _SCAN_FLAGS if settings.get(f)]
    if active_flags:
        log.info("Scan flags: " + ", ".join(active_flags))
    else:
        log.info("Scan flags: none (bare scan)")

    scheduler.start()
    log.info("Daemon is running. Waiting for scheduled events…")
    if IS_WINDOWS:
        log.info("Windows process helpers: CREATE_NO_WINDOW spawn, taskkill stop.")

    stop = threading.Event()

    def _request_stop(*_args):
        stop.set()

    try:
        signal.signal(signal.SIGTERM, _request_stop)
    except (ValueError, OSError, AttributeError) as exc:
        log.warning(f"Could not install SIGTERM handler: {exc}")
    try:
        signal.signal(signal.SIGINT, _request_stop)
    except (ValueError, OSError, AttributeError) as exc:
        log.warning(f"Could not install SIGINT handler: {exc}")

    # Windows: also watch for the parent asking us to die via missing PID file
    # rewrite races — poll so we never hang forever if signals are ignored.
    while not stop.wait(timeout=5.0):
        try:
            if os.path.exists(PID_FILE):
                with open(PID_FILE) as f:
                    recorded = int(f.read().strip())
                if recorded != os.getpid():
                    log.info("PID file taken by another daemon — exiting.")
                    break
            else:
                # PID file removed by kill_existing_daemon — exit cleanly.
                log.info("PID file removed — exiting.")
                break
        except (OSError, ValueError):
            pass

    scheduler.shutdown(wait=False)
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                if int(f.read().strip()) == os.getpid():
                    os.remove(PID_FILE)
    except (OSError, ValueError):
        pass
    log.info("Daemon stopped.")


# ---------------------------------------------------------------------------
# Background mode: --after-identify <job_id> <timeout_minutes>
# ---------------------------------------------------------------------------

def run_after_identify(job_id_arg, timeout_arg):
    log = _make_file_logger()
    log.info("=== after-identify subprocess starting ===")

    try:
        cfg = load_config()
    except Exception as exc:
        log.error(f"Cannot read config: {exc}")
        sys.exit(1)

    try:
        stash = make_stash(cfg["server_connection"])
    except Exception as exc:
        log.error(f"Cannot connect to Stash: {exc}")
        sys.exit(1)

    def gql(query, variables=None):
        return call_gql(stash, query, variables)

    job_id = None if job_id_arg == "none" else job_id_arg
    try:
        timeout = int(timeout_arg)
    except (ValueError, TypeError):
        timeout = 120

    # Read scan_paths from the saved settings so identify is scoped consistently
    settings = validate_and_coerce_settings(
        cfg.get("settings", {}), lambda m: log.warning(m)
    )
    scan_paths = settings.get("scan_paths") or None

    wait_for_scan_and_identify(log, gql, job_id, timeout, scan_paths)
    log.info("=== after-identify subprocess done ===")


# ---------------------------------------------------------------------------
# Plugin task modes — exit quickly, never block
# ---------------------------------------------------------------------------

def task_start_scheduler(stash, server_connection, settings):
    # Persist ApiKey so the detached daemon survives Stash restarts.
    save_config(enrich_server_connection(server_connection), settings)
    kill_existing_daemon()
    # Brief pause so Windows releases the old process handles before relaunch.
    time.sleep(0.3)
    launch_detached("--daemon")

    freq = settings["frequency"]
    time_str = settings.get("time_of_day", "??:??")
    dow = settings.get("day_of_week", "")
    tz = settings.get("timezone", "UTC")
    if freq == "hourly":
        schedule_desc = f"every hour at :00 ({tz})"
    elif freq == "cron":
        schedule_desc = f"crontab '{settings['cron_expr']}' ({tz})"
    elif freq == "weekly":
        schedule_desc = f"weekly on {dow.upper()} at {time_str} ({tz})"
    else:
        schedule_desc = f"daily at {time_str} ({tz})"

    # Keep this task short: do not sit in a long confirm loop. Stash holds the
    # plugin job open until this process exits; any inherited-pipe bug plus a
    # wait made "Start Scheduler" appear hung forever.
    alive, pid = False, None
    for _ in range(8):
        time.sleep(0.15)
        alive, pid = daemon_alive()
        if alive:
            break

    if alive:
        msg = (
            f"[Stash Scheduler] Daemon launched (PID {pid}). Schedule: {schedule_desc}. "
            f"Identify after scan: {'yes' if settings['run_identify'] else 'no'}. "
            f"Daemon log: {LOG_FILE}"
        )
        stash.log.info(msg)
        print(json.dumps({"output": f"Scheduler daemon started (PID {pid}, {schedule_desc})."}))
    else:
        # Still return success-ish quickly — daemon may come up a moment later.
        # Check Status can confirm; hanging this task is worse than a race.
        stash.log.warning(
            "[Stash Scheduler] Daemon spawn requested but PID not confirmed yet. "
            f"Check Status / log: {LOG_FILE}"
        )
        print(json.dumps({
            "output": (
                f"Scheduler start requested ({schedule_desc}). "
                f"PID not confirmed yet — use Check Status. Log: {LOG_FILE}"
            )
        }))
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass


def task_stop_scheduler(stash):
    alive, pid = daemon_alive()
    kill_existing_daemon()
    if alive:
        stash.log.info(f"[Stash Scheduler] Stopped daemon (was PID {pid}).")
        print(json.dumps({"output": f"Scheduler daemon stopped (was PID {pid})."}))
    else:
        stash.log.info("[Stash Scheduler] No daemon was running.")
        print(json.dumps({"output": "No scheduler daemon was running."}))


def task_run_now(stash, settings, force_identify=False, server_connection=None):
    gql = lambda q, v=None: call_gql(stash, q, v)
    if library_jobs_busy(gql, stash):
        stash.log.warning(
            "[Stash Scheduler] Not starting Run Now — Stash already has active "
            "library jobs. Wait for the queue to drain, then retry."
        )
        print(json.dumps({
            "output": "Skipped: Stash already has active library jobs in the queue."
        }))
        return

    # Refresh temp config before spawning --after-identify so the child has a
    # current session + ApiKey (Start Scheduler may have been hours/days ago).
    if server_connection is not None:
        save_config(enrich_server_connection(server_connection), settings)

    stash.log.info("[Stash Scheduler] Triggering scan now…")
    try:
        job_id = trigger_scan(stash, gql, settings)
    except Exception as exc:
        stash.log.error(f"[Stash Scheduler] Scan could not be started: {exc}")
        print(json.dumps({"output": f"Error starting scan: {exc}"}))
        return

    run_identify = force_identify or settings["run_identify"]
    timeout = settings["identify_timeout_minutes"]

    if run_identify:
        jid_arg = str(job_id) if job_id else "none"
        launch_detached("--after-identify", jid_arg, str(timeout))
        stash.log.info("[Stash Scheduler] Identify will follow scan completion (background).")
        print(json.dumps({"output": "Scan started. Identify will follow in the background."}))
    else:
        print(json.dumps({"output": "Scan started."}))


def task_check_status(stash):
    alive, pid = daemon_alive()
    status_line = f"Daemon: RUNNING (PID {pid})" if alive else "Daemon: NOT RUNNING"
    recent = tail_log(30)
    output = (
        f"{status_line}\n"
        f"Log file: {LOG_FILE}\n"
        f"Config file: {CONFIG_FILE}\n"
        f"Platform: {'Windows' if IS_WINDOWS else sys.platform}\n\n"
        f"Recent log ({LOG_FILE}):\n{recent}"
    )
    stash.log.info(f"[Stash Scheduler] {status_line}")
    print(json.dumps({"output": output}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Background subprocess modes
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == "--daemon":
            run_daemon()
            return
        if mode == "--after-identify":
            job_id_arg = sys.argv[2] if len(sys.argv) > 2 else "none"
            timeout_arg = sys.argv[3] if len(sys.argv) > 3 else "120"
            run_after_identify(job_id_arg, timeout_arg)
            return
        print(f"[Stash Scheduler] Unknown argument: {mode}", file=sys.stderr)
        sys.exit(1)

    # Plugin task mode
    raw = sys.stdin.read()
    try:
        plugin_input = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[Stash Scheduler] Failed to parse plugin input: {exc}", file=sys.stderr)
        sys.exit(1)

    server_connection = plugin_input.get("server_connection", {})
    task_mode = plugin_input.get("args", {}).get("mode", "start_scheduler")

    try:
        from stashapi.stashapp import StashInterface
        stash = StashInterface(server_connection)
    except ImportError:
        print("[Stash Scheduler] stashapp-tools not installed.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[Stash Scheduler] Could not connect to Stash: {exc}", file=sys.stderr)
        sys.exit(1)

    settings = get_plugin_settings(stash)
    settings = validate_and_coerce_settings(settings, lambda m: stash.log.warning(m))

    if task_mode == "start_scheduler":
        task_start_scheduler(stash, server_connection, settings)
    elif task_mode == "stop_scheduler":
        task_stop_scheduler(stash)
    elif task_mode == "run_now":
        task_run_now(
            stash, settings, force_identify=False, server_connection=server_connection
        )
    elif task_mode == "force_now":
        stash.log.info("[Stash Scheduler] Force mode — identify will always run after scan.")
        task_run_now(
            stash, settings, force_identify=True, server_connection=server_connection
        )
    elif task_mode == "check_status":
        task_check_status(stash)
    else:
        stash.log.error(f"[Stash Scheduler] Unknown task mode: {task_mode!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
