"""Tests for the shared planning stage (ripper/planner.py)."""

from __future__ import annotations

import json
from pathlib import Path

from strophalos.ripper.plan import (
    ClassificationRecord,
    IdentifyOverride,
    PlanBlock,
    TitleMatch,
    build_title_records,
    read_plan,
)
from strophalos.ripper.planner import resolve_output_dir, seed_or_refresh_plan


class TestResolveOutputDir:
    def test_movie_default_path(self, tmp_path: Path):
        out_dir, disc_num = resolve_output_dir(
            disc_type="movie",
            media_type="bd",
            dir_label="TMNT_COLLECTION",
            output=str(tmp_path),
        )
        assert out_dir == str(tmp_path / "movies" / "rips" / "bd" / "TMNT_COLLECTION" / "disc1")
        assert disc_num == 1

    def test_disc_number_bumps(self, tmp_path: Path):
        (tmp_path / "movies" / "rips" / "bd" / "X" / "disc1").mkdir(parents=True)
        out_dir, disc_num = resolve_output_dir(disc_type="movie", media_type="bd", dir_label="X", output=str(tmp_path))
        assert out_dir.endswith("disc2")
        assert disc_num == 2

    def test_music_goes_to_bd_audio_root(self, tmp_path: Path):
        bd_audio = tmp_path / "output-bd"
        out_dir, disc_num = resolve_output_dir(
            disc_type="music",
            media_type="bd",
            dir_label="FFXIV_OST",
            output=str(tmp_path / "archive"),
            output_bd_audio=str(bd_audio),
        )
        assert out_dir == str(bd_audio / "FFXIV_OST" / "disc1")

    def test_music_dvd_without_bd_audio_root(self, tmp_path: Path):
        out_dir, _ = resolve_output_dir(disc_type="music", media_type="dvd", dir_label="CONCERT", output=str(tmp_path))
        assert out_dir == str(tmp_path / "music" / "rips" / "dvd" / "CONCERT" / "disc1")

    def test_tv_box_set_from_disc_label(self, tmp_path: Path):
        out_dir, disc_num = resolve_output_dir(
            disc_type="tv",
            media_type="bd",
            dir_label="whatever",
            disc_label="MRROBOT_S2D1_NA",
            output=str(tmp_path),
        )
        assert "Season 02" in out_dir
        assert out_dir.endswith("Disc 01")
        assert disc_num == 1

    def test_tv_box_set_falls_back_to_label(self, tmp_path: Path):
        out_dir, _ = resolve_output_dir(
            disc_type="tv",
            media_type="dvd",
            dir_label="x",
            disc_label=None,
            label="Mr. Robot: Season Two (Disc 1)",
            output=str(tmp_path),
        )
        assert "Season 02" in out_dir

    def test_box_set_collision_bumps_only_nonempty(self, tmp_path: Path):
        out1, _ = resolve_output_dir(
            disc_type="tv",
            media_type="bd",
            dir_label="x",
            disc_label="MRROBOT_S2D1_NA",
            output=str(tmp_path),
        )
        # Empty dir exists → same path reused
        Path(out1).mkdir(parents=True)
        out2, _ = resolve_output_dir(
            disc_type="tv",
            media_type="bd",
            dir_label="x",
            disc_label="MRROBOT_S2D1_NA",
            output=str(tmp_path),
        )
        assert out2 == out1
        # Non-empty → bumped
        (Path(out1) / "title_t00.mkv").write_bytes(b"x")
        out3, _ = resolve_output_dir(
            disc_type="tv",
            media_type="bd",
            dir_label="x",
            disc_label="MRROBOT_S2D1_NA",
            output=str(tmp_path),
        )
        assert out3 == f"{out1} (2)"

    def test_tv_non_box_set_default_path(self, tmp_path: Path):
        out_dir, _ = resolve_output_dir(disc_type="tv", media_type="bd", dir_label="SOME_SHOW", output=str(tmp_path))
        assert out_dir == str(tmp_path / "tv" / "rips" / "bd" / "SOME_SHOW" / "disc1")


def _classification() -> ClassificationRecord:
    return ClassificationRecord(disc_type="movie", reason="movie=0.9", suggested_titles_to_rip=[0])


def _titles():
    return build_title_records(
        durations={0: 5556, 1: 5280},
        chapters={0: 12, 1: 11},
        sizes={},
        source_filenames={},
        segment_maps={},
    )


class TestSeedOrRefreshPlan:
    def test_seeds_new_plan_from_default_block(self, tmp_path: Path):
        plan = seed_or_refresh_plan(
            out_dir=str(tmp_path),
            disc_id="d1",
            id_type="bd_sha256",
            disc_label="X",
            media_type="bd",
            title_records=_titles(),
            classification=_classification(),
            existing=None,
            default_block=PlanBlock(disc_type="movie", titles_to_rip=[0]),
        )
        assert plan.plan.titles_to_rip == [0]
        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert loaded.disc_id == "d1"

    def test_refresh_preserves_user_blocks(self, tmp_path: Path):
        existing = seed_or_refresh_plan(
            out_dir=str(tmp_path),
            disc_id="d1",
            id_type="bd_sha256",
            disc_label="X",
            media_type="bd",
            title_records=_titles(),
            classification=_classification(),
            existing=None,
            default_block=PlanBlock(disc_type="movie", titles_to_rip=[0]),
        )
        # User edits while disc is out
        existing.plan.titles_to_rip = [0, 1]
        existing.identify = IdentifyOverride(
            matches=[TitleMatch(url="https://www.themoviedb.org/movie/1498-tmnt", titles=[0])]
        )

        refreshed = seed_or_refresh_plan(
            out_dir=str(tmp_path),
            disc_id="d1",
            id_type="bd_sha256",
            disc_label="X",
            media_type="bd",
            title_records=_titles(),
            classification=_classification(),
            existing=existing,
            default_block=PlanBlock(disc_type="movie", titles_to_rip=[0]),
        )
        assert refreshed.plan.titles_to_rip == [0, 1]
        assert refreshed.identify.matches[0].url == "https://www.themoviedb.org/movie/1498-tmnt"
        # Derived fields materialized on rewrite
        data = json.loads((tmp_path / ".rip-plan.json").read_text())
        assert data["identify"]["matches"][0]["provider"] == "tmdb"
        assert data["identify"]["matches"][0]["id"] == "1498"

    def test_refresh_updates_titles_and_classification(self, tmp_path: Path):
        existing = seed_or_refresh_plan(
            out_dir=str(tmp_path),
            disc_id="d1",
            id_type="bd_sha256",
            disc_label="X",
            media_type="bd",
            title_records=_titles(),
            classification=_classification(),
            existing=None,
            default_block=PlanBlock(disc_type="movie", titles_to_rip=[0]),
        )
        new_cls = ClassificationRecord(disc_type="movie", reason="movie=0.7", suggested_titles_to_rip=[1])
        refreshed = seed_or_refresh_plan(
            out_dir=str(tmp_path),
            disc_id="d1",
            id_type="bd_sha256",
            disc_label="X",
            media_type="bd",
            title_records=_titles(),
            classification=new_cls,
            existing=existing,
            default_block=PlanBlock(disc_type="movie", titles_to_rip=[9]),
        )
        assert refreshed.classification.reason == "movie=0.7"
        assert refreshed.plan.titles_to_rip == [0]  # default_block ignored when existing


def test_write_then_seed_roundtrip_via_disk(tmp_path: Path):
    """Simulates the two-pass loop: seed, hand-edit on disk, refresh."""
    seed_or_refresh_plan(
        out_dir=str(tmp_path),
        disc_id="d1",
        id_type="bd_sha256",
        disc_label="DUAL",
        media_type="bd",
        title_records=_titles(),
        classification=_classification(),
        existing=None,
        default_block=PlanBlock(disc_type="movie", titles_to_rip=[0]),
    )
    data = json.loads((tmp_path / ".rip-plan.json").read_text())
    data["plan"]["titles_to_rip"] = [0, 1]
    data["identify"]["matches"] = [
        {"url": "https://themoviedb.org/movie/1498", "titles": [0]},
        {"url": "https://themoviedb.org/movie/8845", "titles": [1]},
    ]
    (tmp_path / ".rip-plan.json").write_text(json.dumps(data))

    edited = read_plan(tmp_path)
    assert edited is not None
    refreshed = seed_or_refresh_plan(
        out_dir=str(tmp_path),
        disc_id="d1",
        id_type="bd_sha256",
        disc_label="DUAL",
        media_type="bd",
        title_records=_titles(),
        classification=_classification(),
        existing=edited,
        default_block=PlanBlock(disc_type="movie", titles_to_rip=[0]),
    )
    assert refreshed.plan.titles_to_rip == [0, 1]
    assert len(refreshed.identify.matches) == 2
