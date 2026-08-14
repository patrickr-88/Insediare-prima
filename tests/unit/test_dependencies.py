"""Dependency ordering and cycle detection."""

from __future__ import annotations

import pytest

from usbinstaller.engine.dependencies import find_cycles, resolve_order
from usbinstaller.errors import CircularDependencyError, DependencyError


class TestResolveOrder:
    def test_dependencies_come_first(self):
        graph = {"app": ["runtime"], "runtime": []}
        assert resolve_order(["app"], graph) == ["runtime", "app"]

    def test_transitive_dependencies_are_pulled_in(self):
        graph = {"a": ["b"], "b": ["c"], "c": [], "unrelated": []}
        assert resolve_order(["a"], graph) == ["c", "b", "a"]

    def test_diamond_dependencies_appear_once(self):
        graph = {"top": ["left", "right"], "left": ["base"], "right": ["base"], "base": []}
        order = resolve_order(["top"], graph)
        assert order.count("base") == 1
        assert order.index("base") < order.index("left")
        assert order[-1] == "top"

    def test_selection_order_breaks_ties(self):
        graph = {"a": [], "b": [], "c": []}
        assert resolve_order(["c", "a", "b"], graph) == ["c", "a", "b"]

    def test_include_dependencies_false_keeps_only_the_selection(self):
        graph = {"app": ["runtime"], "runtime": []}
        assert resolve_order(["app"], graph, include_dependencies=False) == ["app"]

    def test_ordering_is_still_correct_without_pulling_extras_in(self):
        graph = {"app": ["runtime"], "runtime": []}
        assert resolve_order(
            ["app", "runtime"], graph, include_dependencies=False
        ) == ["runtime", "app"]

    def test_unknown_selection_raises(self):
        with pytest.raises(DependencyError, match="unknown application"):
            resolve_order(["ghost"], {"a": []})

    def test_unknown_dependency_raises(self):
        with pytest.raises(DependencyError, match="unknown application"):
            resolve_order(["a"], {"a": ["ghost"]})

    def test_direct_cycle_raises(self):
        with pytest.raises(CircularDependencyError):
            resolve_order(["a"], {"a": ["b"], "b": ["a"]})

    def test_long_cycle_raises_and_names_the_members(self):
        graph = {"a": ["b"], "b": ["c"], "c": ["a"]}
        with pytest.raises(CircularDependencyError) as exc:
            resolve_order(["a"], graph)
        message = str(exc.value)
        assert all(node in message for node in ("a", "b", "c"))

    def test_cycle_outside_the_selection_does_not_block(self):
        graph = {"clean": [], "x": ["y"], "y": ["x"]}
        assert resolve_order(["clean"], graph) == ["clean"]

    def test_empty_selection(self):
        assert resolve_order([], {"a": []}) == []

    def test_duplicates_in_the_selection_are_collapsed(self):
        assert resolve_order(["a", "a"], {"a": []}) == ["a"]


class TestFindCycles:
    def test_no_cycles(self):
        assert find_cycles({"a": ["b"], "b": []}) == []

    def test_self_loop(self):
        assert find_cycles({"a": ["a"]}) == [["a", "a"]]

    def test_two_node_cycle_is_reported_once(self):
        cycles = find_cycles({"a": ["b"], "b": ["a"]})
        assert len(cycles) == 1
        assert set(cycles[0]) == {"a", "b"}

    def test_multiple_independent_cycles(self):
        graph = {"a": ["b"], "b": ["a"], "c": ["d"], "d": ["c"], "e": []}
        assert len(find_cycles(graph)) == 2

    def test_cycle_normalisation_is_stable(self):
        assert find_cycles({"b": ["c"], "c": ["b"]}) == find_cycles(
            {"c": ["b"], "b": ["c"]}
        )

    def test_deep_graph_does_not_recurse(self):
        """A 5000-node chain must not blow the Python recursion limit."""
        graph = {f"n{i}": [f"n{i + 1}"] for i in range(5000)}
        graph["n5000"] = []
        assert find_cycles(graph) == []
        assert resolve_order(["n0"], graph)[-1] == "n0"
