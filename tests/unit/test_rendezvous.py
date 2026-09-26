from hypothesis import given
from hypothesis import strategies as st

from perimeter.domain.rendezvous import assignment, owner

members = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12),
    min_size=1,
    max_size=8,
    unique=True,
)


def test_no_members_means_no_owner() -> None:
    assert owner(3, []) is None


@given(members=members)
def test_assignment_is_a_partition_of_all_partitions(members: list[str]) -> None:
    result = assignment(16, members)
    owned = [p for parts in result.values() for p in parts]
    assert sorted(owned) == list(range(16))
    assert set(result) == set(members)


@given(members=members)
def test_assignment_does_not_depend_on_member_order(members: list[str]) -> None:
    assert assignment(16, members) == assignment(16, list(reversed(members)))


@given(members=members, newcomer=st.text(alphabet="XYZ", min_size=3, max_size=6))
def test_only_partitions_won_by_a_newcomer_move(members: list[str], newcomer: str) -> None:
    before = assignment(64, members)
    after = assignment(64, [*members, newcomer])
    for member in members:
        assert after[member] <= before[member]
        assert before[member] - after[member] <= after[newcomer]


def test_distribution_is_reasonably_even() -> None:
    result = assignment(256, [f"engine-{i}" for i in range(4)])
    sizes = sorted(len(parts) for parts in result.values())
    assert sizes[0] >= 40
    assert sizes[-1] <= 90
