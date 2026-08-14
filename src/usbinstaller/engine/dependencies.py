"""Dependency ordering with deterministic output and cycle reporting.

A plain Kahn topological sort would be enough to order the graph, but a
technician needs to know *which* applications form a cycle, not just that one
exists — so cycle detection is a separate, explicit pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from ..errors import CircularDependencyError, DependencyError

Graph = Mapping[str, Sequence[str]]


def find_cycles(graph: Graph) -> list[list[str]]:
    """Return every dependency cycle as a node list ending where it started.

    Uses an iterative depth-first search (no recursion limit to trip over on a
    large catalogue). Each cycle is reported once, normalised to start at its
    lexicographically smallest member so the output is stable.
    """
    cycles: set[tuple[str, ...]] = set()
    visited: set[str] = set()

    for start in sorted(graph):
        if start in visited:
            continue
        # stack entries: (node, iterator index) with an explicit path
        path: list[str] = []
        on_path: set[str] = set()
        stack: list[tuple[str, int]] = [(start, 0)]

        while stack:
            node, index = stack[-1]
            if index == 0:
                if node in on_path:
                    cycle = path[path.index(node) :]
                    cycles.add(_normalise_cycle(cycle))
                    stack.pop()
                    continue
                visited.add(node)
                path.append(node)
                on_path.add(node)

            neighbours = list(graph.get(node, ()))
            if index < len(neighbours):
                stack[-1] = (node, index + 1)
                stack.append((neighbours[index], 0))
            else:
                stack.pop()
                on_path.discard(node)
                if path and path[-1] == node:
                    path.pop()

    return [list(c) + [c[0]] for c in sorted(cycles)]


def _normalise_cycle(cycle: Sequence[str]) -> tuple[str, ...]:
    if not cycle:
        return ()
    pivot = min(range(len(cycle)), key=lambda i: cycle[i])
    return tuple(cycle[pivot:]) + tuple(cycle[:pivot])


def resolve_order(
    selected: Iterable[str],
    graph: Graph,
    *,
    include_dependencies: bool = True,
) -> list[str]:
    """Return *selected* (plus transitive dependencies) in installation order.

    Args:
        selected: application ids the technician chose.
        graph: ``{app_id: [dependency_id, ...]}`` for the whole catalogue.
        include_dependencies: when False, dependencies outside *selected* are
            not pulled in — but ordering among the selected set is still
            correct.

    Raises:
        DependencyError: a referenced dependency is not in the catalogue.
        CircularDependencyError: the reachable subgraph contains a cycle.
    """
    wanted = list(dict.fromkeys(selected))
    unknown = [n for n in wanted if n not in graph]
    if unknown:
        raise DependencyError(f"unknown application id(s): {', '.join(sorted(unknown))}")

    # Expand to the reachable set (dependencies of dependencies).
    reachable: list[str] = []
    seen: set[str] = set()
    queue = list(wanted)
    while queue:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        reachable.append(node)
        for dep in graph.get(node, ()):
            if dep not in graph:
                raise DependencyError(
                    f"{node!r} depends on unknown application {dep!r}"
                )
            if dep not in seen:
                queue.append(dep)

    subgraph = {n: [d for d in graph.get(n, ()) if d in seen] for n in reachable}
    cycles = find_cycles(subgraph)
    if cycles:
        raise CircularDependencyError(
            "circular dependency detected: "
            + "; ".join(" -> ".join(c) for c in cycles)
        )

    # Kahn's algorithm, breaking ties by the technician's original order so the
    # plan reads predictably.
    priority = {node: i for i, node in enumerate(reachable)}
    remaining = {n: set(subgraph[n]) for n in reachable}
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            (n for n, deps in remaining.items() if not deps),
            key=lambda n: priority[n],
        )
        if not ready:  # pragma: no cover - cycles are caught above
            raise CircularDependencyError(
                "circular dependency detected among: "
                + ", ".join(sorted(remaining))
            )
        for node in ready:
            ordered.append(node)
            del remaining[node]
        for deps in remaining.values():
            deps.difference_update(ready)

    if not include_dependencies:
        wanted_set = set(wanted)
        return [n for n in ordered if n in wanted_set]
    return ordered
