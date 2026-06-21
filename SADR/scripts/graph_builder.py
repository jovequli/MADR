#!/usr/bin/env python3
"""LangGraph graph builder for SADR calibration.

Connects all node functions from graph_nodes into a StateGraph with
conditional edges and the batch-level loop for topological processing.

Workers are parallelized via ThreadPoolExecutor within the
execute_batch_workers node (not via Send()), avoiding state-copy overhead.

Graph structure:
    START → init_calibration → round_start
                  ↓
             prepare_batch
                  ↓
        execute_batch_workers (ThreadPoolExecutor)
                  ↓
        collect_batch_results
                  ↓
           advance_batch
                  ↓
           [more topo levels?] ─yes→ prepare_batch
                  │ no
                  ↓
      post_round_diagnostics
                  ↓
           leader_review
                  ↓
        update_search_state
                  ↓
           record_round
                  ↓
      check_phase_transition
                  ↓
    check_round_continuation
                  ↓
       [continue?] ─yes→ round_start
          │ no
          ↓
       finalize → END
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from .graph_state import CalibrationState
from . import graph_nodes as N


def build_calibration_graph() -> StateGraph:
    """Build the SADR calibration graph.

    Returns an uncompiled StateGraph.
    """
    graph = StateGraph(CalibrationState)

    # ── Add nodes ──
    graph.add_node("init_calibration", N.init_calibration)
    graph.add_node("check_exploration", N.check_exploration)
    graph.add_node("run_exploration", N.run_exploration)
    graph.add_node("round_start", N.round_start)
    graph.add_node("prepare_batch", N.prepare_batch)
    graph.add_node("execute_batch_workers", N.execute_batch_workers)
    graph.add_node("collect_batch_results", N.collect_batch_results)
    graph.add_node("advance_batch", N.advance_batch)
    graph.add_node("post_round_diagnostics", N.post_round_diagnostics)
    graph.add_node("leader_review", N.leader_review)
    graph.add_node("update_search_state", N.update_search_state)
    graph.add_node("record_round", N.record_round)
    graph.add_node("check_phase_transition", N.check_phase_transition_node)
    graph.add_node("check_round_continuation", N.check_round_continuation)
    graph.add_node("finalize", N.finalize)

    # ── Start ──
    graph.add_edge(START, "init_calibration")
    graph.add_edge("init_calibration", "check_exploration")

    # ── Exploration branch ──
    graph.add_conditional_edges(
        "check_exploration",
        N.needs_exploration,
        {
            "run_exploration": "run_exploration",
            "round_start": "round_start",
        },
    )
    graph.add_edge("run_exploration", "round_start")

    # ── Round loop ──
    graph.add_edge("round_start", "prepare_batch")

    # ── Batch workers (parallel via ThreadPoolExecutor inside node) ──
    graph.add_edge("prepare_batch", "execute_batch_workers")
    graph.add_edge("execute_batch_workers", "collect_batch_results")

    # ── After collecting batch results, check for more batches ──
    graph.add_edge("collect_batch_results", "advance_batch")
    graph.add_conditional_edges(
        "advance_batch",
        N.check_more_batches,
        {
            "prepare_batch": "prepare_batch",
            "post_round_diagnostics": "post_round_diagnostics",
        },
    )

    # ── Post-batch processing chain ──
    graph.add_edge("post_round_diagnostics", "leader_review")
    graph.add_edge("leader_review", "update_search_state")
    graph.add_edge("update_search_state", "record_round")
    graph.add_edge("record_round", "check_phase_transition")
    graph.add_edge("check_phase_transition", "check_round_continuation")

    # ── Continue or finalize ──
    graph.add_conditional_edges(
        "check_round_continuation",
        N.should_continue,
        {
            "round_start": "round_start",
            "finalize": "finalize",
        },
    )
    graph.add_edge("finalize", END)

    return graph


def compile_graph(checkpointer=None):
    """Build and compile the graph, optionally with a checkpointer.

    Args:
        checkpointer: LangGraph checkpointer (e.g., SqliteSaver) for
                      automatic checkpoint/resume. None for no checkpointing.

    Returns:
        Compiled graph ready for .invoke() or .stream().
    """
    graph = build_calibration_graph()
    return graph.compile(checkpointer=checkpointer)
