"""Tests for identify-movie sanity checks (token overlap, false-match guard)."""

from __future__ import annotations

from strophalos.cli.identify_movie import _resolved_title_plausible, _tokens


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
