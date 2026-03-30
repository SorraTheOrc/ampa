"""Engine factory — extracted from scheduler.py.

Constructs a fully-wired ``Engine`` instance from shell-based adapters
and the workflow descriptor.  This is infrastructure wiring, not
scheduling logic.

Canonical imports::

    from ampa.engine_factory import build_engine
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional, Tuple

import subprocess

try:
    from . import fallback
    from .engine.core import Engine, EngineConfig, EngineResult, EngineStatus
    from .engine.descriptor import load_descriptor
    from .engine.candidates import CandidateSelector
    from .engine.dispatch import OpenCodeRunDispatcher
    from .engine.invariants import InvariantEvaluator
    from .engine.adapters import (
        ShellCandidateFetcher,
        ShellInProgressQuerier,
        ShellWorkItemFetcher,
        ShellWorkItemUpdater,
        ShellCommentWriter,
        StoreDispatchRecorder,
        DiscordNotificationSender,
    )
except ImportError:  # pragma: no cover - allow running as script
    import importlib
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    fallback = importlib.import_module("ampa.fallback")
    _ec = importlib.import_module("ampa.engine.core")
    Engine = _ec.Engine
    EngineConfig = _ec.EngineConfig
    EngineResult = _ec.EngineResult
    EngineStatus = _ec.EngineStatus
    load_descriptor = importlib.import_module("ampa.engine.descriptor").load_descriptor
    CandidateSelector = importlib.import_module(
        "ampa.engine.candidates"
    ).CandidateSelector
    InvariantEvaluator = importlib.import_module(
        "ampa.engine.invariants"
    ).InvariantEvaluator
    OpenCodeRunDispatcher = importlib.import_module(
        "ampa.engine.dispatch"
    ).OpenCodeRunDispatcher
    _adapters = importlib.import_module("ampa.engine.adapters")
    ShellCandidateFetcher = _adapters.ShellCandidateFetcher
    ShellInProgressQuerier = _adapters.ShellInProgressQuerier
    ShellWorkItemFetcher = _adapters.ShellWorkItemFetcher
    ShellWorkItemUpdater = _adapters.ShellWorkItemUpdater
    ShellCommentWriter = _adapters.ShellCommentWriter
    StoreDispatchRecorder = _adapters.StoreDispatchRecorder
    DiscordNotificationSender = _adapters.DiscordNotificationSender

LOG = logging.getLogger("ampa.scheduler")


def build_engine(
    run_shell: Callable[..., subprocess.CompletedProcess],
    command_cwd: str,
    store: Any,
) -> Tuple[Optional[Any], Optional[CandidateSelector]]:
    """Construct an Engine from the workflow descriptor.

    Returns a ``(engine, candidate_selector)`` tuple.  Both are ``None``
    when the descriptor cannot be loaded (e.g. the YAML file is missing
    or malformed).

    Parameters
    ----------
    run_shell:
        Callable used to execute shell commands (for wl CLI adapters).
    command_cwd:
        Working directory for shell commands.
    store:
        ``SchedulerStore`` instance (used by ``StoreDispatchRecorder``).
    """
    try:
        descriptor_path = os.getenv(
            "AMPA_WORKFLOW_DESCRIPTOR",
            os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "docs",
                "workflow",
                "workflow.yaml",
            ),
        )

        descriptor = load_descriptor(descriptor_path)

        # Shell-based adapters for wl CLI calls
        fetcher = ShellWorkItemFetcher(
            run_shell=run_shell,
            command_cwd=command_cwd,
        )
        candidate_fetcher = ShellCandidateFetcher(
            run_shell=run_shell,
            command_cwd=command_cwd,
        )
        in_progress_querier = ShellInProgressQuerier(
            run_shell=run_shell,
            command_cwd=command_cwd,
        )
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=candidate_fetcher,
            in_progress_querier=in_progress_querier,
        )
        evaluator = InvariantEvaluator(
            invariants=descriptor.invariants,
            querier=in_progress_querier,
        )
        dispatcher = OpenCodeRunDispatcher(cwd=command_cwd)

        # Protocol adapters for external dependencies
        updater = ShellWorkItemUpdater(
            run_shell=run_shell,
            command_cwd=command_cwd,
        )
        comment_writer = ShellCommentWriter(
            run_shell=run_shell,
            command_cwd=command_cwd,
        )
        recorder = StoreDispatchRecorder(store=store)
        notifier = DiscordNotificationSender()

        # Resolve fallback mode at engine init time so it is consistent
        # for the lifetime of this scheduler instance.
        try:
            fb_mode = fallback.resolve_mode(None, require_config=True)
        except Exception:
            fb_mode = None

        engine_config = EngineConfig(
            descriptor_path=descriptor_path,
            fallback_mode=fb_mode,
        )

        engine = Engine(
            descriptor=descriptor,
            dispatcher=dispatcher,
            candidate_selector=selector,
            invariant_evaluator=evaluator,
            work_item_fetcher=fetcher,
            updater=updater,
            comment_writer=comment_writer,
            dispatch_recorder=recorder,
            notifier=notifier,
            config=engine_config,
        )
        LOG.info(
            "Engine initialized with descriptor=%s fallback_mode=%s",
            descriptor_path,
            fb_mode,
        )
        return engine, selector
    except Exception:
        LOG.exception("Failed to initialize engine")
        return None, None
