"""Explicit Retrieval Specialist state machine."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import append_jsonl


class RetrievalState(StrEnum):
    INIT = "INIT"
    LOAD_PROTOCOL = "LOAD_PROTOCOL"
    BUILD_QUERY = "BUILD_QUERY"
    COMPILE_QUERY = "COMPILE_QUERY"
    SEARCH_SOURCES = "SEARCH_SOURCES"
    NORMALIZE = "NORMALIZE"
    DEDUPLICATE = "DEDUPLICATE"
    RULE_PREFILTER = "RULE_PREFILTER"
    SCREEN_PASS_1 = "SCREEN_PASS_1"
    ENRICH_DEFERRED = "ENRICH_DEFERRED"
    SCREEN_PASS_2 = "SCREEN_PASS_2"
    PERSIST_FINAL_SCREENING = "PERSIST_FINAL_SCREENING"
    EMIT_DOWNLOAD_HANDOFF = "EMIT_DOWNLOAD_HANDOFF"
    VERIFY_HANDOFF = "VERIFY_HANDOFF"
    EVALUATE_QUERY = "EVALUATE_QUERY"
    MINE_TERMS = "MINE_TERMS"
    PROPOSE_VARIANTS = "PROPOSE_VARIANTS"
    TEST_VARIANTS = "TEST_VARIANTS"
    SELECT_QUERY = "SELECT_QUERY"
    ACCEPT_QUERY = "ACCEPT_QUERY"
    REJECT_QUERY = "REJECT_QUERY"
    ROLLBACK = "ROLLBACK"
    CHECK_SATURATION = "CHECK_SATURATION"
    EXPORT_RESULTS = "EXPORT_RESULTS"
    FINALIZE_ITERATION = "FINALIZE_ITERATION"
    RELEASE_ITERATION_MEMORY = "RELEASE_ITERATION_MEMORY"
    PAUSED_MEMORY_LIMIT = "PAUSED_MEMORY_LIMIT"
    PAUSED_DOWNSTREAM_BACKPRESSURE = "PAUSED_DOWNSTREAM_BACKPRESSURE"
    STOP = "STOP"
    FAILED = "FAILED"


class InvalidStateTransition(RuntimeError):
    """Raised when the workflow attempts an invalid state transition."""


VALID_TRANSITIONS: dict[RetrievalState, set[RetrievalState]] = {
    RetrievalState.INIT: {RetrievalState.LOAD_PROTOCOL, RetrievalState.FAILED},
    RetrievalState.LOAD_PROTOCOL: {RetrievalState.BUILD_QUERY, RetrievalState.FAILED},
    RetrievalState.BUILD_QUERY: {RetrievalState.COMPILE_QUERY, RetrievalState.FAILED},
    RetrievalState.COMPILE_QUERY: {RetrievalState.SEARCH_SOURCES, RetrievalState.FAILED},
    RetrievalState.SEARCH_SOURCES: {RetrievalState.NORMALIZE, RetrievalState.FAILED},
    RetrievalState.NORMALIZE: {RetrievalState.DEDUPLICATE, RetrievalState.FAILED},
    RetrievalState.DEDUPLICATE: {RetrievalState.RULE_PREFILTER, RetrievalState.FAILED},
    RetrievalState.RULE_PREFILTER: {RetrievalState.SCREEN_PASS_1, RetrievalState.FAILED},
    RetrievalState.SCREEN_PASS_1: {RetrievalState.ENRICH_DEFERRED, RetrievalState.FAILED},
    RetrievalState.ENRICH_DEFERRED: {RetrievalState.SCREEN_PASS_2, RetrievalState.FAILED},
    RetrievalState.SCREEN_PASS_2: {RetrievalState.PERSIST_FINAL_SCREENING, RetrievalState.FAILED},
    RetrievalState.PERSIST_FINAL_SCREENING: {
        RetrievalState.EMIT_DOWNLOAD_HANDOFF,
        RetrievalState.FAILED,
    },
    RetrievalState.EMIT_DOWNLOAD_HANDOFF: {
        RetrievalState.VERIFY_HANDOFF,
        RetrievalState.PAUSED_DOWNSTREAM_BACKPRESSURE,
        RetrievalState.FAILED,
    },
    RetrievalState.VERIFY_HANDOFF: {RetrievalState.EVALUATE_QUERY, RetrievalState.FAILED},
    RetrievalState.EVALUATE_QUERY: {RetrievalState.MINE_TERMS, RetrievalState.FAILED},
    RetrievalState.MINE_TERMS: {RetrievalState.PROPOSE_VARIANTS, RetrievalState.FAILED},
    RetrievalState.PROPOSE_VARIANTS: {RetrievalState.TEST_VARIANTS, RetrievalState.FAILED},
    RetrievalState.TEST_VARIANTS: {RetrievalState.SELECT_QUERY, RetrievalState.FAILED},
    RetrievalState.SELECT_QUERY: {
        RetrievalState.ACCEPT_QUERY,
        RetrievalState.REJECT_QUERY,
        RetrievalState.ROLLBACK,
        RetrievalState.FAILED,
    },
    RetrievalState.ACCEPT_QUERY: {RetrievalState.CHECK_SATURATION, RetrievalState.FAILED},
    RetrievalState.REJECT_QUERY: {RetrievalState.ROLLBACK, RetrievalState.FAILED},
    RetrievalState.ROLLBACK: {RetrievalState.CHECK_SATURATION, RetrievalState.FAILED},
    RetrievalState.CHECK_SATURATION: {RetrievalState.EXPORT_RESULTS, RetrievalState.FAILED},
    RetrievalState.EXPORT_RESULTS: {RetrievalState.FINALIZE_ITERATION, RetrievalState.FAILED},
    RetrievalState.FINALIZE_ITERATION: {
        RetrievalState.RELEASE_ITERATION_MEMORY,
        RetrievalState.FAILED,
    },
    RetrievalState.RELEASE_ITERATION_MEMORY: {
        RetrievalState.BUILD_QUERY,
        RetrievalState.STOP,
        RetrievalState.FAILED,
    },
    RetrievalState.PAUSED_MEMORY_LIMIT: {
        RetrievalState.LOAD_PROTOCOL,
        RetrievalState.SEARCH_SOURCES,
        RetrievalState.NORMALIZE,
        RetrievalState.PERSIST_FINAL_SCREENING,
        RetrievalState.FAILED,
    },
    RetrievalState.PAUSED_DOWNSTREAM_BACKPRESSURE: {
        RetrievalState.BUILD_QUERY,
        RetrievalState.SEARCH_SOURCES,
        RetrievalState.PERSIST_FINAL_SCREENING,
        RetrievalState.FAILED,
    },
    RetrievalState.STOP: set(),
    RetrievalState.FAILED: set(),
}


class StateMachine:
    """Validate and log Retrieval Specialist state transitions."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.state = RetrievalState.INIT

    def transition(self, next_state: RetrievalState) -> None:
        if next_state not in VALID_TRANSITIONS[self.state]:
            error = (
                f"Invalid Retrieval Specialist transition: {self.state.value} -> {next_state.value}"
            )
            append_jsonl(
                self.run_dir / "logs" / "error_log.jsonl",
                {"timestamp": utc_now_iso(), "error": error},
            )
            raise InvalidStateTransition(error)
        previous = self.state
        self.state = next_state
        append_jsonl(
            self.run_dir / "logs" / "state_transition_log.jsonl",
            {
                "timestamp": utc_now_iso(),
                "previous_state": previous.value,
                "next_state": next_state.value,
            },
        )
