from engine.grid import GridManager, GridDiff
from engine.curve import GridLevel


def test_diff_empty_to_target():
    """Empty current grid -> all target levels are 'place'."""
    target = [
        GridLevel(price=3010.0, size=0.001, side="buy", target_short=0.046),
        GridLevel(price=2990.0, size=0.001, side="sell", target_short=0.048),
    ]
    gm = GridManager()
    diff = gm.diff(current=[], target=target)
    assert len(diff.to_place) == 2
    assert len(diff.to_cancel) == 0


def test_diff_target_empty_cancels_all():
    current = [
        ("hb-r1-l5-1", GridLevel(price=3010.0, size=0.001, side="buy", target_short=0.046)),
    ]
    gm = GridManager()
    diff = gm.diff(current=current, target=[])
    assert len(diff.to_place) == 0
    assert len(diff.to_cancel) == 1
    assert diff.to_cancel[0] == "hb-r1-l5-1"


def test_diff_keeps_matching_orders():
    """When target level matches existing cloid, keep both."""
    level = GridLevel(price=3010.0, size=0.001, side="buy", target_short=0.046)
    current = [("hb-r1-l5-1", level)]
    target = [level]
    gm = GridManager()
    diff = gm.diff(current=current, target=target)
    assert len(diff.to_place) == 0
    assert len(diff.to_cancel) == 0
