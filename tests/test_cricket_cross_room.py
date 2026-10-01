from pathlib import Path

from cricket_game import CricketGame


def test_cross_room_match_starts_after_two_full_rooms(tmp_path: Path):
    game = CricketGame(tmp_path / "cricket")
    game.set_enabled("Room A", True)
    assert game.begin_setup("Room A") is None
    assert game.select_player_count("Room A", 2) is None

    for user in ("a1", "a2"):
        assert game.join("Room A", user) is None
    for user in ("b1", "b2"):
        assert game.join("Room B", user) is None

    state = game.current()
    assert state["stage"] == "teams"
    assert len(state["rooms"]) == 2

    assert game.choose_team("Room A", "attack") is None
    assert game.choose_team("Room B", "defense") is None
    assert game.current()["stage"] == "live"

    rooms = {item["key"]: item["players"] for item in game.current()["rooms"]}
    assert rooms["room a"] == ["a1", "a2"]
    assert rooms["room b"] == ["b1", "b2"]
