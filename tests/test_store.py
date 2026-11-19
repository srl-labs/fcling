from __future__ import annotations

from pathlib import Path

from fcli.delta import Change, Op
from fcli.store import Store


def test_a_batch_is_logged_in_order_and_folded_into_current(tmp_path: Path) -> None:
    """Written in a few statements whatever its size: a statement per change made a
    scale-up's write take minutes on a server whose other threads were busy."""
    store = Store(tmp_path / "h.db")
    odd = '["mac-vrf \\"1\\"","ü/1"]'  # quotes, backslashes and more than ASCII in a key
    first = store.write(
        [
            Change(1.0, "leaf1", "ni", odd, Op.ADD, after={"ni": 'mac-vrf "1"', "vteps": ["10.0.1.3"]}),
            Change(1.0, "leaf1", "ni", '["b"]', Op.ADD, after={"ni": "b"}),
            Change(1.0, "leaf1", "ni", '["c"]', Op.UPDATE, after={"ni": "c"}),  # not kept
        ]
    )
    assert first == [1, 2]
    second = store.write(
        [
            Change(2.0, "leaf1", "ni", odd, Op.CHANGE, before={"ni": 'mac-vrf "1"'}, after={"ni": "x"}),
            Change(2.0, "leaf1", "ni", '["b"]', Op.REMOVE, before={"ni": "b"}),
            Change(2.0, "leaf2", "ni", '["b"]', Op.ADD, after={"ni": "b"}),
        ]
    )
    assert second == [3, 4, 5]
    assert store.current() == {("leaf1", "ni"): {odd: {"ni": "x"}}, ("leaf2", "ni"): {'["b"]': {"ni": "b"}}}
    history = store.history()
    assert [(c["seq"], c["node"], c["key"], c["op"]) for c in history] == [
        (1, "leaf1", odd, "add"),
        (2, "leaf1", '["b"]', "add"),
        (3, "leaf1", odd, "change"),
        (4, "leaf1", '["b"]', "remove"),
        (5, "leaf2", '["b"]', "add"),
    ]
    assert history[0]["after"] == {"ni": 'mac-vrf "1"', "vteps": ["10.0.1.3"]} and history[3]["after"] is None
    assert store.write([]) == []
    store.close()
