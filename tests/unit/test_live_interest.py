from itertools import product

from hypothesis import given, settings
from hypothesis import strategies as st

from perimeter.api.live.interest import InterestTrie

DEPTH = 4
LEAVES = ["".join(digits) for digits in product("0123", repeat=DEPTH)]

prefixes = st.text(alphabet="0123", max_size=DEPTH)
keys = st.integers(min_value=0, max_value=4)
operations = st.lists(
    st.tuples(keys, st.one_of(st.none(), st.frozensets(prefixes, max_size=4))),
    max_size=40,
)


def minimal_cover(held: dict[int, frozenset[str]]) -> set[str]:
    everything = set().union(*held.values()) if held else set()
    return {p for p in everything if not any(p != q and p.startswith(q) for q in everything)}


def test_an_ancestor_takes_over_its_descendants_and_gives_them_back() -> None:
    trie: InterestTrie[str] = InterestTrie()
    first = trie.update("a", ["0120", "0121"])
    assert first.subscribe == {"0120", "0121"}
    assert first.added == {"0120", "0121"}
    zoom_out = trie.update("b", ["01"])
    assert zoom_out.subscribe == {"01"}
    assert zoom_out.unsubscribe == {"0120", "0121"}
    assert trie.cover == {"01"}
    zoom_in = trie.remove("b")
    assert zoom_in.unsubscribe == {"01"}
    assert zoom_in.subscribe == {"0120", "0121"}
    assert zoom_in.removed == {"01"}


def test_a_frame_reaches_every_session_holding_one_of_its_ancestors_once() -> None:
    trie: InterestTrie[str] = InterestTrie()
    trie.update("city", ["0120"])
    trie.update("country", ["01"])
    trie.update("world", [""])
    trie.update("elsewhere", ["3"])
    assert trie.targets("012033") == {"city", "country", "world"}
    assert trie.targets("0130") == {"country", "world"}
    assert trie.targets("3000") == {"elsewhere", "world"}
    assert trie.cover == {""}


def test_shared_prefixes_are_reference_counted() -> None:
    trie: InterestTrie[int] = InterestTrie()
    assert trie.update(1, ["02"]).subscribe == {"02"}
    assert trie.update(2, ["02"]).subscribe == frozenset()
    assert trie.remove(1).unsubscribe == frozenset()
    assert trie.remove(2).unsubscribe == {"02"}
    assert trie.cover == frozenset()
    assert trie.targets("0213") == set()


def test_moving_a_viewport_only_touches_the_edges() -> None:
    trie: InterestTrie[str] = InterestTrie()
    trie.update("s", ["00", "01"])
    change = trie.update("s", ["01", "10"])
    assert change.added == change.subscribe == {"10"}
    assert change.removed == change.unsubscribe == {"00"}
    assert trie.prefixes("s") == {"01", "10"}


@settings(max_examples=300)
@given(ops=operations)
def test_subscriptions_always_equal_the_minimal_cover(
    ops: list[tuple[int, frozenset[str] | None]],
) -> None:
    trie: InterestTrie[int] = InterestTrie()
    held: dict[int, frozenset[str]] = {}
    subscribed: set[str] = set()
    for key, wanted in ops:
        before = held.get(key, frozenset())
        change = trie.remove(key) if wanted is None else trie.update(key, wanted)
        if wanted:
            held[key] = wanted
        else:
            held.pop(key, None)
        after = held.get(key, frozenset())
        assert change.added == after - before
        assert change.removed == before - after
        assert not change.subscribe & change.unsubscribe
        assert change.subscribe.isdisjoint(subscribed), "subscribed twice"
        assert change.unsubscribe <= subscribed, "unsubscribed something never subscribed"
        subscribed = (subscribed - change.unsubscribe) | change.subscribe
        assert subscribed == trie.cover == minimal_cover(held)
        for key_, prefixes_ in held.items():
            assert trie.prefixes(key_) == prefixes_


@settings(max_examples=300)
@given(ops=operations)
def test_each_frame_reaches_exactly_the_sessions_that_want_it_through_one_subscription(
    ops: list[tuple[int, frozenset[str] | None]],
) -> None:
    trie: InterestTrie[int] = InterestTrie()
    held: dict[int, frozenset[str]] = {}
    for key, wanted in ops:
        if wanted is None:
            trie.remove(key)
            held.pop(key, None)
        else:
            trie.update(key, wanted)
            held[key] = wanted
    cover = trie.cover
    for leaf in LEAVES:
        expected = {key for key, ps in held.items() if any(leaf.startswith(p) for p in ps)}
        assert trie.targets(leaf) == expected
        matching = [p for p in cover if leaf.startswith(p)]
        assert len(matching) == (1 if expected else 0)


@given(ops=operations)
def test_releasing_everything_leaves_no_residue(
    ops: list[tuple[int, frozenset[str] | None]],
) -> None:
    trie: InterestTrie[int] = InterestTrie()
    for key, wanted in ops:
        trie.update(key, wanted or ())
    for key in range(5):
        trie.remove(key)
    assert trie.cover == frozenset()
    assert trie._root.children == {}
    assert not trie._root.holders
