"""Tests for the per-disc rip plan (ripper/plan.py)."""

from __future__ import annotations

import json
from pathlib import Path

from strophalos.ripper.plan import (
    ClassificationRecord,
    IdentifyOverride,
    PlanBlock,
    TitleRecord,
    build_plan,
    build_title_records,
    find_plan_by_disc_id,
    plan_path,
    read_plan,
    write_plan,
)


def _sample_classification() -> ClassificationRecord:
    return ClassificationRecord(
        disc_type="tv",
        reason="tv=1.20 > movie=0.20",
        suggested_titles_to_rip=[6, 7, 8, 9, 16],
    )


def _sample_titles() -> list[TitleRecord]:
    return build_title_records(
        durations={0: 2626, 1: 2620, 6: 1293, 16: 789},
        chapters={0: 4, 1: 4, 6: 3, 16: 2},
        sizes={0: 7_730_941_132, 1: 7_700_000_000, 6: 800_000_000, 16: 500_000_000},
        source_filenames={0: "00000.mpls", 1: "00001.mpls"},
        segment_maps={0: "63", 1: "64"},
        dropped={42: "low-bitrate-2.0Mbps-vs-25.0Mbps"},
    )


def _make_plan(disc_id: str = "abc123", override: bool = False, titles_to_rip: list[int] | None = None):
    block = PlanBlock(
        disc_type="tv",
        titles_to_rip=titles_to_rip if titles_to_rip is not None else [6, 7, 8, 9, 16],
        manual_override=override,
    )
    return build_plan(
        disc_id=disc_id,
        id_type="bd_sha256",
        disc_label="TEST",
        media_type="bd",
        titles=_sample_titles(),
        classification=_sample_classification(),
        plan_block=block,
    )


class TestBuildTitleRecords:
    def test_kept_and_dropped_titles_combined(self):
        records = _sample_titles()
        tids = [r.tid for r in records]
        assert tids == [0, 1, 6, 16, 42]
        dropped = next(r for r in records if r.tid == 42)
        assert dropped.dropped == "low-bitrate-2.0Mbps-vs-25.0Mbps"
        assert dropped.duration_seconds == 0

    def test_size_gb_rounds(self):
        records = _sample_titles()
        assert abs(records[0].size_gb - 7.2) < 0.05

    def test_duration_formatted(self):
        records = _sample_titles()
        assert records[0].duration == "0:43:46"


class TestRoundTrip:
    def test_write_then_read(self, tmp_path: Path):
        plan = _make_plan(override=True, titles_to_rip=[0, 1])
        plan.plan.note = "hand-picked"
        write_plan(tmp_path, plan)

        assert plan_path(tmp_path).exists()
        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert loaded.disc_id == "abc123"
        assert loaded.plan.manual_override is True
        assert loaded.plan.titles_to_rip == [0, 1]
        assert loaded.plan.note == "hand-picked"
        assert loaded.classification.suggested_titles_to_rip == [6, 7, 8, 9, 16]
        assert {t.tid for t in loaded.titles} == {0, 1, 6, 16, 42}


class TestIdentifyOverride:
    def test_default_empty_block_roundtrips(self, tmp_path: Path):
        write_plan(tmp_path, _make_plan())
        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert loaded.identify.tmdb_id is None
        assert loaded.identify.tmdb_type is None
        assert loaded.identify.season is None

    def test_pinned_tv_match_roundtrips(self, tmp_path: Path):
        plan = _make_plan()
        plan.identify = IdentifyOverride(
            tmdb_id=71365,
            tmdb_type="tv",
            season=1,
            note="BSG miniseries pinned to S1",
        )
        write_plan(tmp_path, plan)
        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert loaded.identify.tmdb_id == 71365
        assert loaded.identify.tmdb_type == "tv"
        assert loaded.identify.season == 1
        assert loaded.identify.note == "BSG miniseries pinned to S1"

    def test_missing_identify_block_in_legacy_file(self, tmp_path: Path):
        # Pre-feature plan files don't have an "identify" key
        path = plan_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        plan = _make_plan()
        data = json.loads(json.dumps(plan, default=lambda o: o.__dict__))
        data.pop("identify", None)
        path.write_text(json.dumps(data))
        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert loaded.identify.tmdb_id is None


class TestCorruptFile:
    def test_corrupt_json_returns_none(self, tmp_path: Path):
        (tmp_path / ".rip-plan.json").write_text("{not json")
        assert read_plan(tmp_path) is None

    def test_missing_returns_none(self, tmp_path: Path):
        assert read_plan(tmp_path) is None


class TestFindByDiscId:
    def test_finds_plan_in_nested_rip_dir(self, tmp_path: Path):
        rip_dir = tmp_path / "tv" / "rips" / "bd" / "BSG" / "Season 04" / "Disc 02"
        write_plan(rip_dir, _make_plan(disc_id="bsg-d2", override=True, titles_to_rip=[0, 1, 2, 3, 4]))

        hit = find_plan_by_disc_id(tmp_path, "bsg-d2")
        assert hit is not None
        found_dir, found_plan = hit
        assert found_dir == rip_dir
        assert found_plan.plan.manual_override is True
        assert found_plan.plan.titles_to_rip == [0, 1, 2, 3, 4]

    def test_returns_none_when_no_match(self, tmp_path: Path):
        rip_dir = tmp_path / "x" / "disc1"
        write_plan(rip_dir, _make_plan(disc_id="other"))
        assert find_plan_by_disc_id(tmp_path, "missing") is None

    def test_returns_none_when_archive_missing(self, tmp_path: Path):
        assert find_plan_by_disc_id(tmp_path / "nope", "any") is None

    def test_skips_corrupt_plans(self, tmp_path: Path):
        good_dir = tmp_path / "good"
        bad_dir = tmp_path / "bad"
        good_dir.mkdir()
        bad_dir.mkdir()
        (bad_dir / ".rip-plan.json").write_text("{not json")
        write_plan(good_dir, _make_plan(disc_id="found"))

        hit = find_plan_by_disc_id(tmp_path, "found")
        assert hit is not None
        assert hit[0] == good_dir

    def test_corrupt_only_returns_none(self, tmp_path: Path):
        rip_dir = tmp_path / "bad"
        rip_dir.mkdir()
        (rip_dir / ".rip-plan.json").write_text("{not json")
        # disc_id is inside the file, so corrupt entries can't match any id
        assert find_plan_by_disc_id(tmp_path, "whatever") is None


class TestWritePlanCreatesDir:
    def test_creates_nested_dir(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c"
        write_plan(target, _make_plan())
        assert (target / ".rip-plan.json").exists()
        data = json.loads((target / ".rip-plan.json").read_text())
        assert data["disc_id"] == "abc123"
