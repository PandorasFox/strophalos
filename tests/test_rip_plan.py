"""Tests for the per-disc rip plan (ripper/plan.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strophalos.ripper.plan import (
    ClassificationRecord,
    IdentifyOverride,
    PlanBlock,
    PlanValidationError,
    TitleMatch,
    TitleRecord,
    build_plan,
    build_title_records,
    effective_pin,
    find_plan_by_disc_id,
    locate_plan,
    materialize_match_fields,
    parse_provider_url,
    plan_path,
    read_plan,
    validate_plan,
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


def _make_plan(disc_id: str = "abc123", titles_to_rip: list[int] | None = None):
    block = PlanBlock(
        disc_type="tv",
        titles_to_rip=titles_to_rip if titles_to_rip is not None else [6, 7, 8, 9, 16],
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
        plan = _make_plan(titles_to_rip=[0, 1])
        plan.plan.note = "hand-picked"
        write_plan(tmp_path, plan)

        assert plan_path(tmp_path).exists()
        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert loaded.disc_id == "abc123"
        assert loaded.plan.titles_to_rip == [0, 1]
        assert loaded.plan.note == "hand-picked"
        assert loaded.classification.suggested_titles_to_rip == [6, 7, 8, 9, 16]
        assert {t.tid for t in loaded.titles} == {0, 1, 6, 16, 42}

    def test_legacy_flags_ignored_on_load(self, tmp_path: Path):
        # Plans written before the manual_override/forced_rip_all cleanup
        # carry those extra keys.  read_plan should drop them silently.
        path = plan_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(json.dumps(_make_plan(titles_to_rip=[0, 1]), default=lambda o: o.__dict__))
        data["plan"]["manual_override"] = True
        data["plan"]["forced_rip_all"] = True
        path.write_text(json.dumps(data))

        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert loaded.plan.titles_to_rip == [0, 1]
        assert not hasattr(loaded.plan, "manual_override")
        assert not hasattr(loaded.plan, "forced_rip_all")


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
        write_plan(rip_dir, _make_plan(disc_id="bsg-d2", titles_to_rip=[0, 1, 2, 3, 4]))

        hit = find_plan_by_disc_id(tmp_path, "bsg-d2")
        assert hit is not None
        found_dir, found_plan = hit
        assert found_dir == rip_dir
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


_MB_UUID = "8a4c6f2e-1234-4abc-9def-0123456789ab"


class TestParseProviderUrl:
    def test_tmdb_movie_with_slug(self):
        p = parse_provider_url("https://www.themoviedb.org/movie/603-the-matrix")
        assert (p.provider, p.kind, p.id, p.season) == ("tmdb", "movie", "603", None)

    def test_tmdb_movie_without_slug(self):
        p = parse_provider_url("https://themoviedb.org/movie/1498")
        assert (p.provider, p.kind, p.id) == ("tmdb", "movie", "1498")

    def test_tmdb_tv_with_season(self):
        p = parse_provider_url("https://www.themoviedb.org/tv/1396-breaking-bad/season/2")
        assert (p.provider, p.kind, p.id, p.season) == ("tmdb", "tv", "1396", 2)

    def test_http_and_trailing_slash(self):
        p = parse_provider_url("http://themoviedb.org/tv/1396/")
        assert (p.kind, p.id, p.season) == ("tv", "1396", None)

    def test_musicbrainz_release(self):
        p = parse_provider_url(f"https://musicbrainz.org/release/{_MB_UUID}")
        assert (p.provider, p.kind, p.id) == ("musicbrainz", "release", _MB_UUID)

    def test_self_hosted_musicbrainz_release(self):
        # Release UUIDs are portable across mirrors — self-hosted MB URLs pin
        # the same release as musicbrainz.org ones.
        p = parse_provider_url(f"https://mb.example.internal/release/{_MB_UUID.upper()}/")
        assert (p.provider, p.kind, p.id) == ("musicbrainz", "release", _MB_UUID)

    def test_unknown_host_rejected(self):
        with pytest.raises(PlanValidationError):
            parse_provider_url("https://www.imdb.com/title/tt0100758/")

    def test_garbage_rejected(self):
        with pytest.raises(PlanValidationError):
            parse_provider_url("not a url")

    def test_tmdb_person_path_rejected(self):
        with pytest.raises(PlanValidationError):
            parse_provider_url("https://www.themoviedb.org/person/6384-keanu-reeves")


class TestTitleMatchRoundTrip:
    def test_matches_roundtrip(self, tmp_path: Path):
        plan = _make_plan(titles_to_rip=[3, 4])
        plan.plan.disc_type = "movie"
        plan.identify.matches = [
            TitleMatch(url="https://www.themoviedb.org/movie/1498-tmnt", titles=[3]),
            TitleMatch(url="https://www.themoviedb.org/movie/8845-tmnt-ii", titles=[4], note="second feature"),
        ]
        write_plan(tmp_path, plan)
        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert len(loaded.identify.matches) == 2
        assert loaded.identify.matches[0].titles == [3]
        assert loaded.identify.matches[1].note == "second feature"

    def test_unknown_match_keys_filtered(self, tmp_path: Path):
        plan = _make_plan()
        write_plan(tmp_path, plan)
        data = json.loads(plan_path(tmp_path).read_text())
        data["identify"]["matches"] = [{"url": "https://themoviedb.org/movie/603", "titles": [0], "bogus_key": True}]
        data["identify"]["future_field"] = "x"
        plan_path(tmp_path).write_text(json.dumps(data))
        loaded = read_plan(tmp_path)
        assert loaded is not None
        assert loaded.identify.matches[0].url == "https://themoviedb.org/movie/603"

    def test_materialize_fills_derived_fields(self):
        identify = IdentifyOverride(
            matches=[
                TitleMatch(url="https://www.themoviedb.org/tv/1396/season/2", titles=[0]),
                TitleMatch(url="garbage", titles=[1]),
            ]
        )
        materialize_match_fields(identify)
        m = identify.matches[0]
        assert (m.provider, m.kind, m.id, m.season) == ("tmdb", "tv", "1396", 2)
        assert identify.matches[1].provider is None  # unparseable left for validate_plan


class TestEffectivePin:
    def test_none_when_unset(self):
        assert effective_pin(IdentifyOverride()) is None

    def test_url_pin(self):
        pin = effective_pin(IdentifyOverride(url="https://themoviedb.org/movie/603"))
        assert pin is not None
        assert (pin.provider, pin.kind, pin.id) == ("tmdb", "movie", "603")

    def test_legacy_fields_fall_back(self):
        pin = effective_pin(IdentifyOverride(tmdb_id=71365, tmdb_type="tv", season=1))
        assert pin is not None
        assert (pin.provider, pin.kind, pin.id, pin.season) == ("tmdb", "tv", "71365", 1)

    def test_url_season_wins_over_field(self):
        pin = effective_pin(IdentifyOverride(url="https://themoviedb.org/tv/1396/season/3", season=1))
        assert pin is not None
        assert pin.season == 3

    def test_field_season_used_when_url_has_none(self):
        pin = effective_pin(IdentifyOverride(url="https://themoviedb.org/tv/1396", season=4))
        assert pin is not None
        assert pin.season == 4

    def test_agreeing_url_and_legacy_ok(self):
        pin = effective_pin(IdentifyOverride(url="https://themoviedb.org/movie/603", tmdb_id=603, tmdb_type="movie"))
        assert pin is not None
        assert pin.id == "603"

    def test_conflicting_url_and_legacy_raises(self):
        with pytest.raises(PlanValidationError):
            effective_pin(IdentifyOverride(url="https://themoviedb.org/movie/603", tmdb_id=8845))

    def test_mb_release_pin(self):
        pin = effective_pin(IdentifyOverride(url=f"https://musicbrainz.org/release/{_MB_UUID}"))
        assert pin is not None
        assert (pin.provider, pin.kind) == ("musicbrainz", "release")


class TestValidatePlan:
    def _movie_plan(self, titles_to_rip: list[int]):
        plan = _make_plan(titles_to_rip=titles_to_rip)
        plan.plan.disc_type = "movie"
        return plan

    def test_clean_plan_passes(self):
        plan = self._movie_plan([3, 4])
        plan.identify.matches = [
            TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3]),
            TitleMatch(url="https://themoviedb.org/movie/8845", titles=[4]),
        ]
        validate_plan(plan)

    def test_bad_url_reported(self):
        plan = self._movie_plan([3])
        plan.identify.matches = [TitleMatch(url="https://imdb.com/title/tt0100758", titles=[3])]
        with pytest.raises(PlanValidationError, match="unrecognized provider URL"):
            validate_plan(plan)

    def test_release_url_on_movie_disc_reported(self):
        plan = self._movie_plan([3])
        plan.identify.matches = [TitleMatch(url=f"https://musicbrainz.org/release/{_MB_UUID}", titles=[3])]
        with pytest.raises(PlanValidationError, match="disc_type"):
            validate_plan(plan)

    def test_tmdb_url_on_music_disc_reported(self):
        plan = _make_plan(titles_to_rip=[0])
        plan.plan.disc_type = "music"
        plan.identify.matches = [TitleMatch(url="https://themoviedb.org/movie/603", titles=[0])]
        with pytest.raises(PlanValidationError, match="disc_type"):
            validate_plan(plan)

    def test_title_outside_plan_reported(self):
        plan = self._movie_plan([3])
        plan.identify.matches = [TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3, 9])]
        with pytest.raises(PlanValidationError, match="not in plan.titles_to_rip"):
            validate_plan(plan)

    def test_duplicate_title_claim_reported(self):
        plan = self._movie_plan([3, 4])
        plan.identify.matches = [
            TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3]),
            TitleMatch(url="https://themoviedb.org/movie/8845", titles=[3]),
        ]
        with pytest.raises(PlanValidationError, match="already claimed"):
            validate_plan(plan)

    def test_scanned_tids_missing_reported(self):
        plan = self._movie_plan([3, 42])
        with pytest.raises(PlanValidationError, match=r"\[42\] not present"):
            validate_plan(plan, scanned_tids={0, 1, 3})

    def test_scanned_tids_present_ok(self):
        plan = self._movie_plan([3])
        validate_plan(plan, scanned_tids={0, 3})

    def test_all_errors_collected(self):
        plan = self._movie_plan([3])
        plan.identify.url = "https://themoviedb.org/movie/603"
        plan.identify.tmdb_id = 999  # conflicts with url
        plan.identify.matches = [
            TitleMatch(url="garbage", titles=[3]),
            TitleMatch(url="https://themoviedb.org/movie/1498", titles=[7]),  # outside plan
        ]
        with pytest.raises(PlanValidationError) as exc:
            validate_plan(plan, scanned_tids={0})  # tid 3 also missing on disc
        msg = str(exc.value)
        assert "disagrees" in msg
        assert "unrecognized provider URL" in msg
        assert "not in plan.titles_to_rip" in msg
        assert "not present" in msg


class TestLocatePlan:
    def test_hit_with_plan(self, tmp_path: Path):
        rip_dir = tmp_path / "movies" / "rips" / "bd" / "X" / "disc1"
        write_plan(rip_dir, _make_plan(disc_id="xyz"))
        hit = locate_plan(tmp_path, "xyz")
        assert hit is not None
        assert hit.rip_dir == rip_dir
        assert hit.plan is not None
        assert hit.error is None

    def test_schema_broken_match_surfaces_error(self, tmp_path: Path):
        rip_dir = tmp_path / "bad"
        write_plan(rip_dir, _make_plan(disc_id="xyz"))
        data = json.loads(plan_path(rip_dir).read_text())
        del data["plan"]["titles_to_rip"]  # required field gone → dataclass load fails
        plan_path(rip_dir).write_text(json.dumps(data))

        hit = locate_plan(tmp_path, "xyz")
        assert hit is not None
        assert hit.plan is None
        assert hit.error is not None and "failed to load" in hit.error
        # And the compat wrapper treats it as no-find:
        assert find_plan_by_disc_id(tmp_path, "xyz") is None

    def test_unparseable_json_skipped(self, tmp_path: Path):
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / ".rip-plan.json").write_text("{not json")
        assert locate_plan(tmp_path, "anything") is None

    def test_searches_multiple_roots(self, tmp_path: Path):
        root_a = tmp_path / "archive"
        root_b = tmp_path / "output-bd"
        write_plan(root_b / "OST" / "disc1", _make_plan(disc_id="music-disc"))
        hit = locate_plan([root_a, root_b], "music-disc")
        assert hit is not None
        assert hit.rip_dir == root_b / "OST" / "disc1"
