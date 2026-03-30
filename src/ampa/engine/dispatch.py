"""Dispatch interface — pluggable fire-and-forget agent session spawning.

Defines an abstract ``Dispatcher`` protocol and three implementations:

- ``OpenCodeRunDispatcher``: spawns ``opencode run`` as a detached subprocess.
- ``ContainerDispatcher``: acquires a pool container and spawns ``opencode run``
  inside it via ``distrobox enter``.
- ``DryRunDispatcher``: records dispatch calls without spawning (for tests).

Usage::

    from ampa.engine.dispatch import OpenCodeRunDispatcher, DispatchResult

    dispatcher = OpenCodeRunDispatcher(cwd="/path/to/project")
    result = dispatcher.dispatch(
        command='opencode run "/intake WL-123 do not ask questions"',
        work_item_id="WL-123",
    )
    assert result.success

Container teardown
------------------
When a ``ContainerDispatcher`` session completes (success or failure), the
container is automatically stopped and removed via :func:`teardown_container`.
The pool claim is then released so the slot becomes available for replenishment.

- Default teardown timeout: 60 s (override via ``AMPA_CONTAINER_TEARDOWN_TIMEOUT``).
- Teardown is idempotent: if the container is already gone the function returns
  successfully without raising.
- On timeout, the error is logged and the container is added to
  ``pool-cleanup.json`` so the host-side watchdog can destroy it on the next
  pool operation.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

LOG = logging.getLogger("ampa.engine.dispatch")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Result of a dispatch attempt.

    Attributes:
        success: Whether the agent session was successfully spawned.
        pid: Process ID of the spawned session (for logging, not waiting).
        error: Error message if spawn failed.
        command: The shell command that was (or would have been) executed.
        work_item_id: The work item ID being dispatched.
        timestamp: When the dispatch occurred (UTC).
        container_id: Optional container identifier when the session was
            dispatched inside a container (e.g. Podman/Distrobox).
    """

    success: bool
    command: str
    work_item_id: str
    timestamp: datetime
    pid: int | None = None
    error: str | None = None
    container_id: str | None = None

    @property
    def summary(self) -> str:
        """One-line human-readable summary."""
        if self.success:
            parts = [f"Dispatched {self.work_item_id} (pid={self.pid}"]
            if self.container_id is not None:
                parts.append(f", container={self.container_id}")
            parts.append(f") at {self.timestamp.isoformat()}")
            return "".join(parts)
        return (
            f"Dispatch failed for {self.work_item_id}: {self.error} "
            f"at {self.timestamp.isoformat()}"
        )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Dispatcher(Protocol):
    """Protocol for fire-and-forget agent session dispatch.

    Implementations must spawn an independent agent session and return
    immediately — they must NOT wait for the session to complete.
    """

    def dispatch(
        self,
        command: str,
        work_item_id: str,
    ) -> DispatchResult:
        """Spawn an independent agent session.

        Args:
            command: The full shell command to execute
                (e.g. ``opencode run "/intake WL-123"``).
            work_item_id: The work item being dispatched (for logging).

        Returns:
            A ``DispatchResult`` indicating spawn success or failure.
        """
        ...


# ---------------------------------------------------------------------------
# OpenCode Run dispatcher (default production implementation)
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return the current UTC time (extracted for testability)."""
    return datetime.now(timezone.utc)


class OpenCodeRunDispatcher:
    """Spawns ``opencode run`` as a detached subprocess.

    The child process is started in a new session (``start_new_session=True``)
    so it survives the engine process exiting.  Stdout and stderr are
    redirected to ``/dev/null`` (or ``NUL`` on Windows) so the engine does not
    block on pipe buffers.

    Args:
        cwd: Working directory for the spawned process.  Defaults to the
            current working directory.
        env: Environment variables for the subprocess.  Defaults to inheriting
            the current environment.
        clock: Callable returning the current UTC datetime (override in tests).
    """

    def __init__(
        self,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        clock: Any = None,
    ) -> None:
        self._cwd = cwd
        self._env = env
        self._clock = clock or _utc_now

    def dispatch(
        self,
        command: str,
        work_item_id: str,
    ) -> DispatchResult:
        """Spawn an independent ``opencode run`` subprocess.

        The process is fully detached:
        - New session via ``start_new_session=True`` (POSIX ``setsid``).
        - Stdout/stderr sent to ``DEVNULL`` so no pipe buffers can block.
        - No waiting — returns immediately after ``Popen`` succeeds.

        Spawn errors (``FileNotFoundError``, ``PermissionError``, ``OSError``,
        etc.) are caught and returned as a failed ``DispatchResult`` rather
        than raised.
        """
        ts = self._clock()
        LOG.info(
            "Dispatching %s: %s (cwd=%s)",
            work_item_id,
            command,
            self._cwd or os.getcwd(),
        )
        try:
            proc = subprocess.Popen(  # noqa: S603 — shell execution is intentional
                command,
                shell=True,  # noqa: S602 — command strings require shell
                cwd=self._cwd,
                env=self._env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            LOG.error("Dispatch spawn failed (file not found): %s", exc)
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"FileNotFoundError: {exc}",
            )
        except PermissionError as exc:
            LOG.error("Dispatch spawn failed (permission denied): %s", exc)
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"PermissionError: {exc}",
            )
        except OSError as exc:
            LOG.error("Dispatch spawn failed (OS error): %s", exc)
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"OSError: {exc}",
            )

        LOG.info(
            "Dispatch successful: %s -> pid %d",
            work_item_id,
            proc.pid,
        )
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            timestamp=ts,
            pid=proc.pid,
        )


# ---------------------------------------------------------------------------
# Container dispatcher (pool-based Podman/Distrobox dispatch)
# ---------------------------------------------------------------------------

# Default timeout (seconds) for the distrobox-enter subprocess.
_DEFAULT_CONTAINER_DISPATCH_TIMEOUT = 30

# Grace period (seconds) between SIGTERM and SIGKILL during timeout enforcement.
_TIMEOUT_GRACE_PERIOD = 5


def _enforce_timeout(proc: subprocess.Popen, timeout: int) -> None:
    """Kill *proc* after *timeout* seconds if it is still running.

    Runs as a daemon-thread callback so ``dispatch()`` can return immediately
    (fire-and-forget contract).  Because the child is started with
    ``start_new_session=True`` we use ``os.killpg`` to terminate the entire
    process group, ensuring any children spawned inside the distrobox
    container are also cleaned up.

    Escalation sequence (mirrors ``BotSupervisor.shutdown``):
      1. ``SIGTERM`` the process group.
      2. Wait up to ``_TIMEOUT_GRACE_PERIOD`` seconds for graceful exit.
      3. ``SIGKILL`` the process group if still alive.
    """
    exit_code = proc.poll()
    if exit_code is not None:
        # Already exited — nothing to do.
        return

    pgid = proc.pid  # process group id == pid when start_new_session=True
    LOG.warning(
        "Container dispatch timeout (%ds) reached for pid %d — sending SIGTERM",
        timeout,
        proc.pid,
    )

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return  # Already gone or we lack permission — nothing more to do.

    try:
        proc.wait(timeout=_TIMEOUT_GRACE_PERIOD)
    except subprocess.TimeoutExpired:
        LOG.warning(
            "Container dispatch pid %d did not exit after SIGTERM + %ds grace — sending SIGKILL",
            proc.pid,
            _TIMEOUT_GRACE_PERIOD,
        )
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # Already gone.


def _global_ampa_dir() -> Path:
    """Return the global AMPA state directory.

    Mirrors the JS ``globalAmpaDir()`` in ampa.mjs:
    ``$XDG_CONFIG_HOME/opencode/.worklog/ampa`` (falls back to
    ``~/.config/opencode/.worklog/ampa``).
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "opencode" / ".worklog" / "ampa"


def _pool_state_path() -> Path:
    """Return the path to the pool state file."""
    return _global_ampa_dir() / "pool-state.json"


def _read_pool_state() -> dict[str, Any]:
    """Read the pool state from disk.

    Returns an empty dict when the file doesn't exist or is invalid.
    """
    p = _pool_state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_pool_state(state: dict[str, Any]) -> None:
    """Persist the pool state to disk (atomic-ish write)."""
    p = _pool_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


_POOL_PREFIX = "ampa-pool-"
_POOL_SIZE = 3
_POOL_MAX_INDEX = _POOL_SIZE * 3  # 9


def _existing_pool_containers() -> set[str]:
    """Return the set of pool container names that currently exist in Podman.

    Uses ``podman ps -a`` with a name filter — mirrors the JS helper.
    """
    try:
        result = subprocess.run(  # noqa: S603, S607
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                f"name={_POOL_PREFIX}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return set()
        return {n for n in result.stdout.strip().split("\n") if n}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return set()


def _list_available_pool() -> list[str]:
    """List pool containers that exist in Podman and are NOT claimed or in cleanup.

    Mirrors the JS ``listAvailablePool`` function, extended to also exclude
    containers that are pending watchdog cleanup (pool-cleanup.json).
    """
    state = _read_pool_state()
    existing = _existing_pool_containers()
    cleanup = set(_read_cleanup_list())
    available: list[str] = []
    for i in range(_POOL_MAX_INDEX):
        name = f"{_POOL_PREFIX}{i}"
        if name in existing and name not in state and name not in cleanup:
            available.append(name)
    return available


def _claim_pool_container(
    work_item_id: str,
    branch: str,
) -> str | None:
    """Claim a pool container for *work_item_id*.

    Writes the claim to ``pool-state.json`` and returns the container name,
    or ``None`` when no pool containers are available.
    """
    available = _list_available_pool()
    if not available:
        return None
    name = available[0]
    state = _read_pool_state()
    state[name] = {
        "workItemId": work_item_id,
        "branch": branch,
        "claimedAt": datetime.now(timezone.utc).isoformat(),
    }
    _save_pool_state(state)
    return name


def _release_pool_container(container_name: str) -> None:
    """Release the claim on *container_name* in ``pool-state.json``."""
    state = _read_pool_state()
    state.pop(container_name, None)
    _save_pool_state(state)


# ---------------------------------------------------------------------------
# Pool cleanup list (pool-cleanup.json)
# ---------------------------------------------------------------------------


def _pool_cleanup_path() -> Path:
    """Return the path to the pool cleanup file.

    Mirrors the JS ``poolCleanupPath()`` in ampa.mjs.
    """
    return _global_ampa_dir() / "pool-cleanup.json"


def _read_cleanup_list() -> list[str]:
    """Read the list of containers marked for watchdog cleanup.

    Returns an empty list when the file doesn't exist or is invalid.
    """
    p = _pool_cleanup_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(item) for item in data]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save_cleanup_list(containers: list[str]) -> None:
    """Persist the cleanup list to disk."""
    p = _pool_cleanup_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(containers, indent=2), encoding="utf-8")


def _mark_for_cleanup(container_name: str) -> None:
    """Add *container_name* to ``pool-cleanup.json`` for watchdog cleanup.

    Idempotent: if the container is already in the list it is not duplicated.
    """
    existing = _read_cleanup_list()
    if container_name not in existing:
        existing.append(container_name)
        _save_cleanup_list(existing)
    LOG.warning(
        "Container %s marked for watchdog cleanup in pool-cleanup.json",
        container_name,
    )


# ---------------------------------------------------------------------------
# Container teardown
# ---------------------------------------------------------------------------

# Default timeout (seconds) for podman stop during container teardown.
_DEFAULT_CONTAINER_TEARDOWN_TIMEOUT = 60

# Extra seconds added to the subprocess.run() timeout beyond the podman --time
# value so the podman process itself has time to relay the stop signal and exit.
_TEARDOWN_STOP_TIMEOUT_BUFFER = 10

# Timeout (seconds) for the podman rm subprocess call.
_TEARDOWN_RM_TIMEOUT = 30

# Maximum number of stderr characters to include in log messages.
_MAX_STDERR_LOG_CHARS = 512


def _is_not_found_error(stderr: str) -> bool:
    """Return ``True`` if *stderr* indicates the container does not exist.

    Used to implement idempotent teardown: if a container has already been
    removed, ``podman stop`` / ``podman rm`` emit a "not found"-style message
    and we treat that as a successful no-op rather than an error.
    """
    lower = stderr.lower()
    return any(
        phrase in lower
        for phrase in (
            "no container with name or id",
            "no such container",
            "container not found",
            "does not exist",
        )
    )


def teardown_container(container_id: str, timeout: int | None = None) -> bool:
    """Stop and remove *container_id* via ``podman stop`` + ``podman rm``.

    Both operations are idempotent: if the container has already been removed
    the function returns ``True`` without raising.

    If ``podman stop`` times out or fails unexpectedly, the error is logged and
    the container is added to ``pool-cleanup.json`` so the host-side watchdog
    can destroy it on the next pool operation.  The function returns ``False``
    in that case.

    Args:
        container_id: Name or ID of the container to tear down.
        timeout: Maximum seconds to wait for ``podman stop`` to complete.
            Defaults to the ``AMPA_CONTAINER_TEARDOWN_TIMEOUT`` environment
            variable, or 60 seconds when the variable is not set / invalid.

    Returns:
        ``True`` when the container was successfully stopped and removed (or
        was already absent), ``False`` when teardown failed (error logged;
        container marked for watchdog cleanup).
    """
    if timeout is None:
        env_val = os.environ.get("AMPA_CONTAINER_TEARDOWN_TIMEOUT")
        if env_val is not None:
            try:
                timeout = int(env_val)
            except ValueError:
                timeout = _DEFAULT_CONTAINER_TEARDOWN_TIMEOUT
        else:
            timeout = _DEFAULT_CONTAINER_TEARDOWN_TIMEOUT

    LOG.info("Tearing down container %s (timeout=%ds)", container_id, timeout)

    # -- Stop ---------------------------------------------------------------
    try:
        stop_result = subprocess.run(  # noqa: S603, S607
            ["podman", "stop", "--time", str(timeout), container_id],
            capture_output=True,
            text=True,
            timeout=timeout + _TEARDOWN_STOP_TIMEOUT_BUFFER,
        )
        if stop_result.returncode != 0:
            stderr = stop_result.stderr or ""
            if _is_not_found_error(stderr):
                LOG.info(
                    "Container %s already removed (stop phase) — treating as success",
                    container_id,
                )
                return True
            LOG.error(
                "podman stop failed for %s (rc=%d): %s",
                container_id,
                stop_result.returncode,
                stderr[:_MAX_STDERR_LOG_CHARS],
            )
            _mark_for_cleanup(container_id)
            return False
    except subprocess.TimeoutExpired:
        LOG.error(
            "podman stop timed out for %s after %ds — marking for watchdog cleanup",
            container_id,
            timeout,
        )
        _mark_for_cleanup(container_id)
        return False
    except (FileNotFoundError, OSError) as exc:
        LOG.error("podman stop failed for %s: %s", container_id, exc)
        _mark_for_cleanup(container_id)
        return False

    # -- Remove -------------------------------------------------------------
    try:
        rm_result = subprocess.run(  # noqa: S603, S607
            ["podman", "rm", container_id],
            capture_output=True,
            text=True,
            timeout=_TEARDOWN_RM_TIMEOUT,
        )
        if rm_result.returncode != 0:
            stderr = rm_result.stderr or ""
            if _is_not_found_error(stderr):
                LOG.info(
                    "Container %s already removed (rm phase) — treating as success",
                    container_id,
                )
                return True
            LOG.error(
                "podman rm failed for %s (rc=%d): %s",
                container_id,
                rm_result.returncode,
                stderr[:_MAX_STDERR_LOG_CHARS],
            )
            _mark_for_cleanup(container_id)
            return False
    except subprocess.TimeoutExpired:
        LOG.error(
            "podman rm timed out for %s — marking for watchdog cleanup",
            container_id,
        )
        _mark_for_cleanup(container_id)
        return False
    except (FileNotFoundError, OSError) as exc:
        LOG.error("podman rm failed for %s: %s", container_id, exc)
        _mark_for_cleanup(container_id)
        return False

    LOG.info("Container %s successfully torn down", container_id)
    return True


def _teardown_on_completion(
    proc: subprocess.Popen,  # type: ignore[type-arg]
    container_name: str,
    teardown_fn: Callable[[str], bool],
    release_fn: Callable[[str], None],
) -> None:
    """Wait for *proc* to exit then tear down *container_name*.

    Designed to run as a daemon thread started immediately after a successful
    :meth:`ContainerDispatcher.dispatch`.  Ensures the container is stopped,
    removed, and the pool claim released regardless of whether the agent exits
    successfully or with an error.

    Args:
        proc: The ``Popen`` object for the running agent session.
        container_name: Pool container to tear down when the session ends.
        teardown_fn: Callable implementing the teardown (default:
            :func:`teardown_container`).  Accepts ``container_name`` and
            returns a bool.  Provided as a parameter so tests can patch it.
        release_fn: Callable that releases the pool claim (default:
            :func:`_release_pool_container`).  Accepts ``container_name``.
    """
    try:
        proc.wait()
    except Exception:
        LOG.exception(
            "Unexpected error waiting for container process (pid=%d, container=%s)",
            proc.pid,
            container_name,
        )
    # Tear down the container regardless of the exit code or any wait error.
    teardown_fn(container_name)
    # Release the pool claim so the slot becomes available for replenishment.
    release_fn(container_name)


class ContainerDispatcher:
    """Acquires a pool container and spawns ``opencode run`` inside it.

    The dispatcher:

    1. Claims an available pool container from ``pool-state.json``.
    2. Launches ``distrobox enter <container> -- opencode run "<prompt>"`` as a
       detached subprocess (new session, DEVNULL stdio).
    3. Returns a ``DispatchResult`` with ``container_id`` set to the container
       name and ``pid`` set to the child process PID.

    On failure the pool claim is released before returning a failed result.

    Args:
        project_root: Project root directory passed into the container via the
            ``AMPA_PROJECT_ROOT`` environment variable.
        branch: Git branch name written into the pool claim record.
        env: Extra environment variables for the subprocess.  The container
            environment variables (``AMPA_CONTAINER_NAME``, etc.) are merged
            on top.
        clock: Callable returning the current UTC datetime (override in tests).
        timeout: Subprocess timeout in seconds.  Overridden by the
            ``AMPA_CONTAINER_DISPATCH_TIMEOUT`` environment variable.
    """

    def __init__(
        self,
        project_root: str | None = None,
        branch: str = "",
        env: dict[str, str] | None = None,
        clock: Any = None,
        timeout: int | None = None,
    ) -> None:
        self._project_root = project_root or os.getcwd()
        self._branch = branch
        self._env = env
        self._clock = clock or _utc_now
        # Timeout: env-var > constructor arg > default.
        env_timeout = os.environ.get("AMPA_CONTAINER_DISPATCH_TIMEOUT")
        if env_timeout is not None:
            try:
                self._timeout = int(env_timeout)
            except ValueError:
                self._timeout = timeout or _DEFAULT_CONTAINER_DISPATCH_TIMEOUT
        else:
            self._timeout = timeout or _DEFAULT_CONTAINER_DISPATCH_TIMEOUT

    # -- Pool helpers (thin wrappers so they can be patched in tests) -------

    @staticmethod
    def _list_available() -> list[str]:
        return _list_available_pool()

    @staticmethod
    def _claim(work_item_id: str, branch: str) -> str | None:
        return _claim_pool_container(work_item_id, branch)

    @staticmethod
    def _release(container_name: str) -> None:
        _release_pool_container(container_name)

    @staticmethod
    def _teardown(container_name: str) -> bool:
        """Tear down *container_name* (thin wrapper for patching in tests)."""
        return teardown_container(container_name)

    # -- Dispatch -----------------------------------------------------------

    def dispatch(
        self,
        command: str,
        work_item_id: str,
    ) -> DispatchResult:
        """Acquire a container and spawn the agent session inside it.

        The command is wrapped as::

            distrobox enter <container> -- <command>

        The child process inherits the current environment, extended with
        container-specific variables (``AMPA_CONTAINER_NAME``,
        ``AMPA_WORK_ITEM_ID``, ``AMPA_BRANCH``, ``AMPA_PROJECT_ROOT``).
        """
        ts = self._clock()

        # 1. Acquire a pool container ------------------------------------
        container_name = self._claim(work_item_id, self._branch)
        if container_name is None:
            LOG.warning("No pool containers available for %s", work_item_id)
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error="No pool containers available",
            )

        LOG.info("Claimed container %s for %s", container_name, work_item_id)

        # 2. Build the distrobox command ---------------------------------
        distrobox_cmd = f"distrobox enter {container_name} -- {command}"

        # Merge container env vars on top of caller-supplied / inherited env.
        spawn_env = dict(self._env) if self._env else dict(os.environ)
        spawn_env.update(
            {
                "AMPA_CONTAINER_NAME": container_name,
                "AMPA_WORK_ITEM_ID": work_item_id,
                "AMPA_BRANCH": self._branch,
                "AMPA_PROJECT_ROOT": self._project_root,
            }
        )

        # 3. Spawn the detached subprocess --------------------------------
        try:
            proc = subprocess.Popen(  # noqa: S603
                distrobox_cmd,
                shell=True,  # noqa: S602
                cwd=self._project_root,
                env=spawn_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            LOG.error(
                "Container dispatch spawn failed for %s (%s): %s",
                work_item_id,
                container_name,
                exc,
            )
            # Release the claim so the container goes back to the pool.
            self._release(container_name)
            return DispatchResult(
                success=False,
                command=distrobox_cmd,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"{type(exc).__name__}: {exc}",
                container_id=container_name,
            )

        # 4. Schedule timeout enforcement -----------------------------------
        if self._timeout and self._timeout > 0:
            timer = threading.Timer(
                self._timeout,
                _enforce_timeout,
                args=(proc, self._timeout),
            )
            timer.daemon = True  # Don't prevent engine shutdown.
            timer.start()

        # 5. Schedule teardown on completion --------------------------------
        # When the agent session finishes (success or failure), a daemon
        # thread stops and removes the container then releases the pool claim.
        teardown_thread = threading.Thread(
            target=_teardown_on_completion,
            args=(proc, container_name, self._teardown, self._release),
            daemon=True,
        )
        teardown_thread.start()

        LOG.info(
            "Container dispatch successful: %s -> container=%s pid=%d timeout=%ds",
            work_item_id,
            container_name,
            proc.pid,
            self._timeout,
        )
        return DispatchResult(
            success=True,
            command=distrobox_cmd,
            work_item_id=work_item_id,
            timestamp=ts,
            pid=proc.pid,
            container_id=container_name,
        )


# ---------------------------------------------------------------------------
# Dry-run dispatcher (for testing / simulation)
# ---------------------------------------------------------------------------


@dataclass
class DispatchRecord:
    """A recorded dispatch call from ``DryRunDispatcher``."""

    command: str
    work_item_id: str
    timestamp: datetime


class DryRunDispatcher:
    """Records dispatch calls without spawning processes.

    Useful for scheduler simulation mode and unit tests.  Every call to
    ``dispatch()`` appends a ``DispatchRecord`` to the ``calls`` list and
    returns a successful ``DispatchResult`` with a synthetic PID.

    Args:
        clock: Callable returning the current UTC datetime (override in tests).
        fail_on: Optional set of work item IDs that should simulate spawn
            failure.  If the dispatched ``work_item_id`` is in this set,
            ``dispatch()`` returns a failed result.
    """

    def __init__(
        self,
        clock: Any = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self._clock = clock or _utc_now
        self._fail_on = fail_on or set()
        self.calls: list[DispatchRecord] = []
        self._next_pid = 10000

    def dispatch(
        self,
        command: str,
        work_item_id: str,
    ) -> DispatchResult:
        """Record the dispatch call and return a mock result."""
        ts = self._clock()
        self.calls.append(
            DispatchRecord(
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
            )
        )

        if work_item_id in self._fail_on:
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"Simulated spawn failure for {work_item_id}",
            )

        pid = self._next_pid
        self._next_pid += 1
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            timestamp=ts,
            pid=pid,
        )
