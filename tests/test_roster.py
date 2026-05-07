"""Tests for src/roster.py — CSV-backed agent loader."""

from src.roster import find_by_email, load_agents


def _write_csv(path, rows):
    path.write_text(rows, encoding="utf-8")
    return path


class TestLoadAgents:
    def test_skips_inactive_rows(self, tmp_path):
        csv = _write_csv(tmp_path / "agents.csv", (
            "name,email,fub_agent_id,active\n"
            "Alice,alice@x,1,1\n"
            "Bob,bob@x,2,0\n"
            "Carol,carol@x,3,\n"        # blank active = include
            "Dan,dan@x,4,false\n"       # 'false' = exclude
        ))
        out = load_agents(csv)
        names = [a["name"] for a in out]
        assert names == ["Alice", "Carol"]

    def test_skips_rows_missing_required_fields(self, tmp_path):
        csv = _write_csv(tmp_path / "agents.csv", (
            "name,email,fub_agent_id,active\n"
            ",no-name@x,1,1\n"
            "No Email,,1,1\n"
            "Good,good@x,1,1\n"
        ))
        out = load_agents(csv)
        assert [a["name"] for a in out] == ["Good"]

    def test_emails_lowercased(self, tmp_path):
        csv = _write_csv(tmp_path / "agents.csv", (
            "name,email,fub_agent_id,active\n"
            "Alice,Alice@Example.COM,1,1\n"
        ))
        out = load_agents(csv)
        assert out[0]["email"] == "alice@example.com"

    def test_returns_empty_when_file_missing(self, tmp_path):
        assert load_agents(tmp_path / "absent.csv") == []

    def test_blank_fub_id_becomes_none(self, tmp_path):
        csv = _write_csv(tmp_path / "agents.csv", (
            "name,email,fub_agent_id,active\n"
            "Alice,alice@x,,1\n"
        ))
        out = load_agents(csv)
        assert out[0]["fub_agent_id"] is None


class TestFindByEmail:
    def test_case_insensitive_match(self):
        agents = [
            {"name": "Alice", "email": "alice@x", "fub_agent_id": "1"},
            {"name": "Bob",   "email": "bob@x",   "fub_agent_id": "2"},
        ]
        a = find_by_email("ALICE@x", agents=agents)
        assert a is not None and a["name"] == "Alice"

    def test_no_match_returns_none(self):
        agents = [{"name": "Alice", "email": "alice@x", "fub_agent_id": "1"}]
        assert find_by_email("zelda@x", agents=agents) is None
