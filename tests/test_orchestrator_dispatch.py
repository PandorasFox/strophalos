"""Tests for the pure video-disc dispatch decision (daemon/orchestrator.py)."""

from __future__ import annotations

from pathlib import Path

from strophalos.daemon.orchestrator import PipelineMode, decide_video_action
from strophalos.ripper.plan import (
    ClassificationRecord,
    PlanBlock,
    PlanHit,
    build_plan,
)


def _hit(valid: bool = True) -> PlanHit:
    if not valid:
        return PlanHit(Path("/x"), None, error="failed to load")
    plan = build_plan(
        disc_id="d1",
        id_type="bd_sha256",
        disc_label="X",
        media_type="bd",
        titles=[],
        classification=ClassificationRecord("movie", "r", [0]),
        plan_block=PlanBlock("movie", [0]),
    )
    return PlanHit(Path("/x"), plan)


class TestDecideVideoAction:
    def test_no_plan_plans(self):
        assert decide_video_action(PipelineMode.FULL, "d1", None) == "plan"
        assert decide_video_action(PipelineMode.RIP, "d1", None) == "plan"

    def test_existing_plan_rips(self):
        assert decide_video_action(PipelineMode.FULL, "d1", _hit()) == "rip"
        assert decide_video_action(PipelineMode.RIP, "d1", _hit()) == "rip"

    def test_no_disc_id_falls_back_single_pass(self):
        assert decide_video_action(PipelineMode.FULL, None, None) == "single-pass"

    def test_scan_mode_always_plans(self):
        # Even with an existing plan, SCAN re-plans (refresh) and ejects.
        assert decide_video_action(PipelineMode.SCAN, "d1", _hit()) == "plan"
        assert decide_video_action(PipelineMode.SCAN, "d1", None) == "plan"
        # No disc_id: plan stage still runs (it retries the id post-scan).
        assert decide_video_action(PipelineMode.SCAN, None, None) == "plan"

    def test_unloadable_plan_rejected(self):
        assert decide_video_action(PipelineMode.FULL, "d1", _hit(valid=False)) == "reject-invalid"
        assert decide_video_action(PipelineMode.SCAN, "d1", _hit(valid=False)) == "reject-invalid"
