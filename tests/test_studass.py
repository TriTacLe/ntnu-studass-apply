import studass


def option(code, label=None):
    return {"code": code, "label": label if label is not None else code, "value": code}


class TestScrub:
    def test_redacts_an_eleven_digit_number(self):
        assert studass.scrub("born 12345678901 here") == "born REDACTED here"

    def test_leaves_shorter_and_longer_runs_alone(self):
        html = "1234567890 and 123456789012"
        assert studass.scrub(html) == html

    def test_redacts_a_fodselsnummer_input_value(self):
        html = '<input name="fodselsnummer" value="12345678901">'
        assert "12345678901" not in studass.scrub(html)

    def test_redacts_a_personnummer_input_even_when_the_value_is_not_eleven_digits(self):
        html = '<input name="personnummer" value="abc">'
        assert studass.scrub(html) == '<input name="personnummer" value="REDACTED">'

    def test_redacts_the_url_encoded_spelling(self):
        html = '<input name="f%C3%B8dselsnummer" value="secret">'
        assert "secret" not in studass.scrub(html)

    def test_leaves_an_unrelated_input_alone(self):
        html = '<input name="comment" value="keep me">'
        assert studass.scrub(html) == html


class TestTier:
    def test_first_bucket_wins_for_an_it_prefix(self):
        assert studass.tier("TDT4100") == 0
        assert studass.tier("IDATT2105") == 0

    def test_later_buckets_rank_in_order(self):
        assert studass.tier("TTM4100") == 1
        assert studass.tier("TTK4105") == 2
        assert studass.tier("TMA4100") == 3

    def test_unknown_prefix_sorts_last(self):
        assert studass.tier("XXX1000") == len(studass.TIERS)


class TestKeep:
    def test_drops_a_test_listing(self):
        assert not studass.keep(option("TDT4100", "TEST listing"), None, None, None)

    def test_only_restricts_to_the_listed_codes(self):
        assert studass.keep(option("TDT4100"), ["TDT4100"], None, None)
        assert not studass.keep(option("TDT4102"), ["TDT4100"], None, None)

    def test_exclude_removes_a_code(self):
        assert not studass.keep(option("TDT4100"), None, ["TDT4100"], None)

    def test_trondheim_drops_a_gjovik_label(self):
        assert not studass.keep(option("TDT4100", "Gjøvik, intro"), None, None, "trondheim")

    def test_trondheim_drops_a_gjovik_code_prefix(self):
        assert not studass.keep(option("IDATG1001", "IDATG1001 intro"), None, None, "trondheim")

    def test_gjovik_survives_when_no_campus_filter_is_given(self):
        assert studass.keep(option("IDATG1001", "IDATG1001 intro"), None, None, None)


class TestOrdered:
    def test_priority_file_wins_over_the_tier_table(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studass, "REPO", tmp_path)
        (tmp_path / "priority.txt").write_text("TMA4100\n", encoding="utf-8")
        result = studass.ordered([option("TDT4100"), option("TMA4100")])
        assert [o["code"] for o in result] == ["TMA4100", "TDT4100"]

    def test_falls_back_to_tiers_without_a_priority_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studass, "REPO", tmp_path)
        result = studass.ordered([option("TMA4100"), option("TTK4105"), option("TDT4100")])
        assert [o["code"] for o in result] == ["TDT4100", "TTK4105", "TMA4100"]

    def test_same_tier_sorts_by_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studass, "REPO", tmp_path)
        result = studass.ordered([option("TDT4102", "b"), option("TDT4100", "a")])
        assert [o["label"] for o in result] == ["a", "b"]

    def test_filtered_options_never_reach_the_plan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studass, "REPO", tmp_path)
        result = studass.ordered([option("TDT4100"), option("TDT4102")], exclude=["TDT4102"])
        assert [o["code"] for o in result] == ["TDT4100"]


class TestPriorityOrder:
    def test_strips_comments_blank_lines_and_case(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studass, "REPO", tmp_path)
        (tmp_path / "priority.txt").write_text(
            "tdt4100  # first\n\n# a whole comment line\nTMA4100\n", encoding="utf-8"
        )
        assert studass.priority_order() == ["TDT4100", "TMA4100"]

    def test_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studass, "REPO", tmp_path)
        assert studass.priority_order() == []


class TestCodesArg:
    def test_splits_trims_and_uppercases(self):
        assert studass.codes_arg(" tdt4100 , idatt2105 ") == ["TDT4100", "IDATT2105"]

    def test_empty_input_is_an_empty_list(self):
        assert studass.codes_arg("") == []
        assert studass.codes_arg(None) == []

    def test_drops_empty_segments(self):
        assert studass.codes_arg("TDT4100,,") == ["TDT4100"]


class TestApplied:
    def test_matches_a_code_inside_a_row(self):
        assert studass.applied(["TDT4100 Objektorientert"], "TDT4100")

    def test_no_match_is_false(self):
        assert not studass.applied(["TMA4100"], "TDT4100")

    def test_empty_code_is_never_applied(self):
        assert not studass.applied(["TDT4100"], "")


class TestHost:
    def test_reads_the_hostname(self):
        assert studass.host("https://idp.feide.no/simplesaml") == "idp.feide.no"

    def test_a_bare_string_has_no_host(self):
        assert studass.host("not a url") == ""


class TestCommentText:
    def test_reads_and_strips_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studass, "REPO", tmp_path)
        (tmp_path / "comment.txt").write_text("  hello  \n", encoding="utf-8")
        assert studass.comment_text() == "hello"

    def test_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studass, "REPO", tmp_path)
        assert studass.comment_text() == ""
