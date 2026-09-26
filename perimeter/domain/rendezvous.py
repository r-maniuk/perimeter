"""Rendezvous (highest-random-weight) hashing of partitions onto engine instances.

Each instance computes the same assignment from the same member list, with no coordinator:
partition ``p`` belongs to the member with the highest ``hash(p, member)``. When a member joins or
leaves, only the partitions that member wins or loses move (about ``P / n`` of them), which keeps
rebalancing cheap and bounded.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from hashlib import blake2b


def weight(partition: int, member: str) -> int:
    digest = blake2b(f"{partition}\x1f{member}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def owner(partition: int, members: Iterable[str]) -> str | None:
    """The member that should own ``partition``; ``None`` when there are no members."""
    best: tuple[int, str] | None = None
    for member in members:
        candidate = (weight(partition, member), member)
        if best is None or candidate > best:
            best = candidate
    return None if best is None else best[1]


def assignment(partitions: int, members: Sequence[str]) -> dict[str, frozenset[int]]:
    """Every member mapped to the partitions it should own (members with none map to empty)."""
    owned: dict[str, set[int]] = {member: set() for member in members}
    for partition in range(partitions):
        chosen = owner(partition, members)
        if chosen is not None:
            owned[chosen].add(partition)
    return {member: frozenset(parts) for member, parts in owned.items()}
