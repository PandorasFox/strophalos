"""Tests for identify-movie sanity checks (token overlap, false-match guard)
and per-title plan-match identification."""

from __future__ import annotations

import json
from pathlib import Path

from strophalos.cli import identify_movie
from strophalos.cli.identify_movie import (
    _identify_by_matches,
    _movie_matches,
    _resolved_title_plausible,
    _tokens,
)
from strophalos.ripper.plan import (
    ClassificationRecord,
    IdentifyOverride,
    PlanBlock,
    TitleMatch,
    build_plan,
    parse_provider_url,
)


class TestTokens:
    def test_lowercased_alnum(self):
        assert _tokens("Halo 22") == {"halo", "22"}

    def test_drops_stopwords(self):
        assert _tokens("The Title of the Movie") == {"movie"}

    def test_drops_short_tokens(self):
        # "a" stop, "to" stop, "go" length<2... wait "go" is 2 chars and not stopword
        assert _tokens("a go to") == {"go"}

    def test_handles_none(self):
        assert _tokens(None) == set()

    def test_punctuation_splits(self):
        assert _tokens("Final_Fantasy: VII!") == {"final", "fantasy", "vii"}


class TestResolvedTitlePlausible:
    def test_match_when_label_overlaps_title(self):
        # FINAL_FANTASY_VII → "Final Fantasy VII: Advent Children" — clear overlap
        assert _resolved_title_plausible(
            label="FINAL_FANTASY_VII",
            mkv_stem="FINAL_FANTASY_VII_t00",
            winning_query="FINAL FANTASY VII",
            resolved_title="Final Fantasy VII: Advent Children",
        )

    def test_match_when_kagi_query_overlaps(self):
        # Catalog code, Kagi resolved it — winning_query carries the signal
        assert _resolved_title_plausible(
            label="HALO_22",
            mkv_stem="title_t00",
            winning_query="Nine Inch Nails Beside You in Time",
            resolved_title="Nine Inch Nails: Beside You in Time",
        )

    def test_reject_catalog_code_runtime_coincidence(self):
        # The actual HALO_22 false match — must reject
        assert not _resolved_title_plausible(
            label="HALO_22",
            mkv_stem="title_t00",
            winning_query="HALO 22",
            resolved_title="Title Shot",
        )

    def test_reject_generic_mkv_stem_picking_random_title(self):
        # MakeMKV gave us just "title_t00"; without label overlap we should bail
        assert not _resolved_title_plausible(
            label="UPK75",
            mkv_stem="title_t00",
            winning_query="UPK75",
            resolved_title="Some Unrelated Movie",
        )

    def test_empty_resolved_title_does_not_block(self):
        # If TMDb gave us nothing to compare, we don't block on this gate alone
        assert _resolved_title_plausible("HALO_22", "title_t00", "HALO 22", "")


_TMDB_MOVIES = {
    1498: {"id": 1498, "title": "Teenage Mutant Ninja Turtles", "release_date": "1990-03-30"},
    8845: {"id": 8845, "title": "Teenage Mutant Ninja Turtles II", "release_date": "1991-03-22"},
}


def _dual_feature_plan(matches: list[TitleMatch]):
    return build_plan(
        disc_id="dual-1",
        id_type="bd_sha256",
        disc_label="TMNT_DOUBLE",
        media_type="bd",
        titles=[],
        classification=ClassificationRecord("movie", "movie=0.4", [3]),
        plan_block=PlanBlock("movie", [3, 4]),
        identify=IdentifyOverride(matches=matches),
    )


def _fake_mkvs(disc_dir: Path, tids: list[int]) -> list[Path]:
    disc_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for tid in tids:
        f = disc_dir / f"title_t{tid:02d}.mkv"
        f.write_bytes(b"x" * (100 + tid))
        files.append(f)
    return sorted(files)


class TestMovieMatches:
    def test_filters_to_movie_kind(self):
        plan = _dual_feature_plan(
            [
                TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3]),
                TitleMatch(url="https://themoviedb.org/tv/1396", titles=[4]),
                TitleMatch(url="garbage", titles=[4]),
            ]
        )
        matches = _movie_matches(plan)
        assert len(matches) == 1
        assert matches[0][1].id == "1498"


class TestIdentifyByMatches:
    def _run(self, tmp_path: Path, monkeypatch, matches: list[TitleMatch], tids: list[int]):
        disc_dir = tmp_path / "archive" / "disc1"
        library = tmp_path / "library"
        mkvs = _fake_mkvs(disc_dir, tids)

        monkeypatch.setattr(identify_movie, "fetch_movie_by_id", lambda i: _TMDB_MOVIES.get(i))
        monkeypatch.setattr(identify_movie, "get_mkv_duration", lambda p: 5400.0)
        monkeypatch.setattr(identify_movie, "notify", lambda *a, **k: None)

        parsed = [(m, parse_provider_url(m.url)) for m in matches]
        _identify_by_matches(disc_dir, library, "TMNT_DOUBLE", parsed, mkvs, dry_run=False)
        return disc_dir, library

    def test_two_movies_linked_to_two_folders(self, tmp_path: Path, monkeypatch):
        disc_dir, library = self._run(
            tmp_path,
            monkeypatch,
            [
                TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3]),
                TitleMatch(url="https://themoviedb.org/movie/8845", titles=[4]),
            ],
            tids=[3, 4],
        )
        assert (
            library / "movies" / "Teenage Mutant Ninja Turtles (1990)" / "Teenage Mutant Ninja Turtles.mkv"
        ).exists()
        assert (
            library / "movies" / "Teenage Mutant Ninja Turtles II (1991)" / "Teenage Mutant Ninja Turtles II.mkv"
        ).exists()

        manifest = json.loads((disc_dir / ".movie-manifest.json").read_text())
        assert len(manifest["movies"]) == 2
        assert manifest["unmatched"] == []
        # Legacy top-level mirror of movies[0]
        assert manifest["title"] == "Teenage Mutant Ninja Turtles"
        assert manifest["tmdb_id"] == 1498

        state = json.loads((disc_dir / ".identify-state.json").read_text())
        assert state["status"] == "success"

    def test_extra_titles_in_same_match_become_extras(self, tmp_path: Path, monkeypatch):
        disc_dir, library = self._run(
            tmp_path,
            monkeypatch,
            [TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3, 4])],
            tids=[3, 4],
        )
        folder = library / "movies" / "Teenage Mutant Ninja Turtles (1990)"
        assert (folder / "Teenage Mutant Ninja Turtles.mkv").exists()
        assert (folder / "Teenage Mutant Ninja Turtles - Extra 1.mkv").exists()

    def test_unmatched_files_listed(self, tmp_path: Path, monkeypatch):
        disc_dir, _library = self._run(
            tmp_path,
            monkeypatch,
            [TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3])],
            tids=[3, 4],  # tid 4 ripped but not matched
        )
        manifest = json.loads((disc_dir / ".movie-manifest.json").read_text())
        assert manifest["unmatched"] == ["title_t04.mkv"]
        state = json.loads((disc_dir / ".identify-state.json").read_text())
        assert state["status"] == "success"

    def test_missing_title_on_disk_partial(self, tmp_path: Path, monkeypatch):
        disc_dir, library = self._run(
            tmp_path,
            monkeypatch,
            [
                TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3]),
                TitleMatch(url="https://themoviedb.org/movie/8845", titles=[9]),  # not on disk
            ],
            tids=[3, 4],
        )
        assert (library / "movies" / "Teenage Mutant Ninja Turtles (1990)").exists()
        state = json.loads((disc_dir / ".identify-state.json").read_text())
        assert state["status"] == "partial"

    def test_unresolvable_tmdb_id_partial(self, tmp_path: Path, monkeypatch):
        disc_dir, _library = self._run(
            tmp_path,
            monkeypatch,
            [
                TitleMatch(url="https://themoviedb.org/movie/1498", titles=[3]),
                TitleMatch(url="https://themoviedb.org/movie/999999", titles=[4]),  # not in fake TMDb
            ],
            tids=[3, 4],
        )
        state = json.loads((disc_dir / ".identify-state.json").read_text())
        assert state["status"] == "partial"
        manifest = json.loads((disc_dir / ".movie-manifest.json").read_text())
        assert len(manifest["movies"]) == 1
