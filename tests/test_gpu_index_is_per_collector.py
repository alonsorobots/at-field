"""A gpu.{n} index means nothing outside the collector that assigned it.

MEASURED on a two-GPU host, 2026-08-31. Loading PHYSICAL gpu0 drove
``gpu.1.mem_junction_temp_c`` from 78.6 to 102.8 C while ``gpu.0``'s sat flat at
77.5; loading physical gpu1 did the exact reverse. NVML numbers GPUs by PCI
order, while the LHM collector hands out ``gpu.{n}`` from a running counter in
sensor-enumeration order -- on that host the two were transposed.

The dispatch loop took the index straight out of the firing signal's NAME and
looked it up in the NVML process map. For an LHM-sourced signal that selected
the processes on the WRONG CARD: the innocent worker was killed while the hot
card kept running. It is also why a host was killed seven times in two hours
and never cooled.

The rule that comes out of it, and what these tests pin: an index may only be
carried into a collector's map when the signal came from THAT collector.
"""
import re

from atfield import service


def _dispatch_source():
    import inspect
    return inspect.getsource(service.run_service)


class TestCrossCollectorIndexIsRefused:
    def test_nvml_map_is_only_used_for_nvml_signals(self):
        src = _dispatch_source()
        # The guard must sit on the same condition that reaches into the map.
        assert re.search(
            r"nvml is not None\s+and effective\.signal\.startswith\(\"gpu\.\"\)\s*\n\s*"
            r"and effective\.signal in nvml_owned_signals", src), (
            "the NVML process map is reachable for a signal NVML did not "
            "produce; its gpu index is a different card's"
        )

    def test_the_owned_signal_set_comes_from_the_probe(self):
        src = _dispatch_source()
        assert "nvml_owned_signals" in src
        assert re.search(r"probe_results\.get\(\"nvml\"\)", src), (
            "the set of NVML-owned signals must come from NVML's own probe "
            "result, not from a hardcoded list that will drift"
        )

    def test_a_foreign_gpu_signal_is_reported_not_silently_dropped(self):
        # Falling back is correct, but silently is how this stayed invisible.
        src = _dispatch_source()
        assert re.search(r"is not an NVML signal", src), (
            "a GPU rule that cannot use the NVML map must say so; silence is "
            "what let mis-targeted kills run for hours"
        )


class TestTheLhmIndexIsAKnownHazard:
    def test_lhm_assigns_indices_by_enumeration_order(self):
        """Pin the actual mechanism, so a future reader sees why this matters.

        If lhmlib ever starts deriving the index from the device identifier
        instead of a counter, this test should be revisited -- but even then the
        orders are not guaranteed to agree with NVML's, so the guard above stays
        the load-bearing part.
        """
        import inspect

        from atfield.collectors import lhmlib
        src = inspect.getsource(lhmlib)
        assert "gpu.{gpu_idx}.mem_junction_temp_c" in src.replace("f\"", "\"")
        assert re.search(r"gpu_idx \+= 1", src), (
            "expected the enumeration-order counter this guard exists for"
        )
