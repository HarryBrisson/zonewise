import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("rasterio")
from shapely.geometry import box  # noqa: E402

from zonewise import snapshot  # noqa: E402
from zonewise.build import load_districts  # noqa: E402

CELL = 10.0  # feet, for a small grid


def grids(polys):
    index, codes, transform = snapshot.rasterize_classes(polys, cell=CELL)
    return snapshot.derive_layers(index, codes, load_districts()), transform


def test_families_and_far_per_cell():
    polys = [("RS-3", box(0, 0, 100, 100)), ("RM-5", box(100, 0, 200, 100)), ("PD-1234", box(200, 0, 300, 100)),
             ("POS-2", box(300, 0, 400, 100))]
    out, _ = grids(polys)
    assert out["zoned_single_family"].shape == (10, 40)
    row = {name: out[name][5, ::10].tolist() for name in out}  # one cell in each polygon
    assert row["zoned_single_family"] == [1, 0, 0, 0]
    assert row["zoned_multifamily"] == [0, 1, 0, 0]
    assert row["zoned_planned_development"] == [0, 0, 1, 0]
    far = row["zoned_max_far"]
    assert far[:2] == [pytest.approx(0.9), pytest.approx(2.0)] and np.isnan(far[2]) and np.isnan(far[3])  # PD, POS set no FAR


def test_off_map_and_unknown_codes_are_nan_so_the_mean_is_over_zoned_land():
    polys = [("RS-1", box(0, 0, 100, 100)), ("B3-2", box(200, 0, 300, 100)), ("XX-9", box(0, 100, 100, 200))]
    out, _ = grids(polys)
    sf = out["zoned_single_family"]
    assert np.isnan(sf[15, 15])        # the gap between polygons: off the map
    assert np.isnan(sf[5, 5])          # unknown district code (row 5 is y 100–200 → the XX-9 box)
    assert np.nanmean(sf) == pytest.approx(0.5)  # RS-1 and B3-2 are equal areas
    assert np.nanmean(out["zoned_business_commercial"]) == pytest.approx(0.5)


def test_grid_snaps_to_cell_multiples():
    transform, (h, w) = snapshot.grid_for((15, 25, 95, 101), cell=CELL)
    assert (transform.c, transform.f, h, w) == (10, 110, 9, 9)


def test_spec_part_dates_every_layer_and_merge_replaces_old_entries():
    layers, metrics = snapshot.spec_part("2026-10-19")
    assert set(layers) == set(snapshot.LAYER_NAMES) and all(layer["as_of"] == "2026-10-19" for layer in layers.values())
    assert all(m["combine"] == "mean" and m["layer"] in layers for m in metrics.values())
    assert {m["scale"] for k, m in metrics.items() if k != "zoned_far_mean"} == {100} and "scale" not in metrics["zoned_far_mean"]
    old = {"layers": {"rezonings": {}, "zoned_max_far": {"as_of": "2026-09-02"}},
           "metrics": {"rezonings_passed_per_km2": {"layer": "rezonings"}, "zoned_far_mean": {"layer": "zoned_max_far"}}}
    merged = snapshot.merge_into_spec(json.loads(json.dumps(old)), "2026-10-19")
    assert merged["layers"]["zoned_max_far"]["as_of"] == "2026-10-19" and "rezonings" in merged["layers"]
    assert snapshot.merge_into_spec(json.loads(json.dumps(old)), None)["metrics"] == {"rezonings_passed_per_km2": {"layer": "rezonings"}}
