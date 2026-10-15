"""Merging one frame's detections into the objects an inspector actually reviews.

A segmentation model run at the confidence threshold this project uses
(``YOLO_CONFIDENCE_THRESHOLD``, deliberately far below Ultralytics' default)
reports *one* crack many times, as overlapping fragments: several windows of the
same damage fire, and each contributes its own polygon. Counting those rows would
report the model's uncertainty as a count of defects on the building, and listing
them one by one would make an inspector confirm the same crack five times.

So the rows are folded into **objects**: one connected piece of damage on one
frame. The fold is computed once, when a frame's detections are written (see
``ImageService.store_defects``), and the result is stored on every row as
``defects.object_index`` - rows that share it are one object. That is
deliberately a *stored* decision rather than something each screen recomputes on
render:

* the workspace, the project cards and the progress screen count the same
  objects, because they read one column instead of running their own geometry;
* a reviewer's edits (confirm, delete, erase) keep the grouping, so a mask cannot
  jump between objects while it is being reviewed - and none of those writes has
  to re-run any of this;
* the geometry runs once per frame, in the worker, next to the inference that
  produced the masks, instead of on every frame change in the browser.

The rule
--------
Two detections of the *same class* belong to one object when their masks come
within :data:`MERGE_DISTANCE` of the frame. Classes never merge with each other:
a crack inside a spalled area is two findings, and grouping them as one would
erase that.

Distances are measured with the max-norm on a :data:`GROUP_CELLS` grid over the
normalized frame - frame fractions rather than pixels, so the grouping does not
depend on the photo's resolution, exactly as the masks themselves do not. The
threshold matches what the previous browser-side implementation produced after
dilating both masks (``2 * round(0.014 * 1024) / 1024``), so a project keeps the
grouping it already had. What changed is *how* it is measured: outlines are
compared instead of filled areas, which is what makes the whole thing affordable
in pure Python - the API image deliberately installs none of the AI stack, and
this module must import there. Containment is therefore checked explicitly, so a
fragment lying *inside* another mask of its class still merges instead of
slipping through the outline test.

The implementation is pure Python on purpose: it runs in the worker (which has
``numpy``/``opencv``) *and* in the Alembic migration that backfills the column on
an existing database (which has neither), so a single implementation decides the
grouping everywhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Cells across the normalized (square) frame for the proximity test.
#:
#: 256 keeps the quantization error at ~0.4% of the frame, an order of magnitude
#: below the gap being measured. The browser used 1024; the same code at that
#: resolution would spend hundreds of milliseconds per frame in Python, and the
#: extra precision buys nothing - the masks are ragged network output, and the
#: tolerance for the result is "these fragments are one crack".
GROUP_CELLS = 256

#: How close two same-class masks must be to be one object, in cells.
#: ``7 / 256`` is 0.027 of the frame.
MERGE_CELLS = 7

#: The same threshold as a frame fraction, for documentation and for callers
#: that need to talk about it in the units the masks are stored in.
MERGE_DISTANCE = MERGE_CELLS / GROUP_CELLS

#: Sampling step along a ring edge, in cells. Half a cell keeps the traced
#: outline within a quarter cell of the real one.
_EDGE_SAMPLES_PER_CELL = 2

#: How many points of a mask's outline are kept for the containment test. A
#: handful is enough: a mask that lies inside another has *all* of its points
#: inside it, so any sample answers the question.
_CONTAINMENT_SAMPLES = 24

#: Neighbourhoods used by the distance test, one per gap, built once: they are
#: walked millions of times over a project's lifetime.
_OFFSETS: dict[int, tuple[tuple[int, int], ...]] = {
    gap: tuple((dx, dy) for dy in range(-gap, gap + 1) for dx in range(-gap, gap + 1))
    for gap in (1, MERGE_CELLS)
}

Point = tuple[float, float]
Box = tuple[int, int, int, int]

#: One input row: its class (the stored enum value) and its mask as stored.
Entry = tuple[str, Any]


def _as_rings(mask: Any) -> list[list[Point]]:
    """Read a stored mask as closed rings of points.

    Accepts both shapes the column holds: a list of rings (what the detector and
    the annotation tools write) and the legacy single ring (a flat list of
    points, written before masks could have several parts). Anything unusable -
    a null mask, a ring with fewer than three vertices - is dropped rather than
    guessed at: a detection without a usable mask simply becomes its own object.
    """
    if not isinstance(mask, (list, tuple)) or not mask:
        return []

    first = mask[0]
    raw_rings: Sequence[Any] = [mask] if isinstance(first, Mapping) else list(mask)

    rings: list[list[Point]] = []
    for raw_ring in raw_rings:
        if not isinstance(raw_ring, (list, tuple)):
            continue
        points: list[Point] = []
        for raw_point in raw_ring:
            if not isinstance(raw_point, Mapping):
                continue
            try:
                points.append((float(raw_point["x"]), float(raw_point["y"])))
            except (KeyError, TypeError, ValueError):
                continue
        if len(points) >= 3:
            rings.append(points)
    return rings


#: Side of one cell of the coarse pre-filter grid, in proximity cells.
_COARSE_STRIDE = MERGE_CELLS

#: Cells per row of the coarse grid. One more than the exact division, because
#: the remainder columns of the fine grid belong to a bucket of their own - and
#: an index that wrapped to the next row would put unrelated cells in the same
#: bucket, which is precisely what the pre-filter must not do.
_COARSE_CELLS = GROUP_CELLS // _COARSE_STRIDE + 1


def _coarse_cell(cell: int) -> int:
    """The pre-filter cell a proximity cell falls in.

    Two outlines within :data:`MERGE_CELLS` on the fine grid are always within
    one cell on this grid, so a pair that lands further apart than that cannot
    merge - which is what lets the expensive exact test be skipped for the vast
    majority of pairs on a frame with a hundred detections.
    """
    x = (cell % GROUP_CELLS) // _COARSE_STRIDE
    y = (cell // GROUP_CELLS) // _COARSE_STRIDE
    return y * _COARSE_CELLS + x


def _cell_of(point: Point) -> int:
    """The grid cell a normalized point falls in, clamped to the frame."""
    limit = GROUP_CELLS - 1
    x = min(limit, max(0, int(point[0] * GROUP_CELLS)))
    y = min(limit, max(0, int(point[1] * GROUP_CELLS)))
    return y * GROUP_CELLS + x


def _outline_points(rings: Sequence[Sequence[Point]]) -> list[Point]:
    """The mask's outline, walked as a point chain and sampled per half cell.

    The chain, not the filled area: two closed shapes are close exactly when
    their boundaries are (or when one lies inside the other, which is tested
    separately), and walking a boundary is proportional to its length instead of
    to the area of its bounding box.
    """
    points: list[Point] = []
    for ring in rings:
        count = len(ring)
        for index in range(count):
            ax, ay = ring[index - 1]
            bx, by = ring[index]
            span = max(abs(bx - ax), abs(by - ay)) * GROUP_CELLS
            steps = max(1, int(span * _EDGE_SAMPLES_PER_CELL) + 1)
            for step in range(steps):
                t = step / steps
                points.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return points


def _box_of(cells: Sequence[int] | set[int], width: int = GROUP_CELLS) -> Box | None:
    """The grid bounding box of a set of cells, or ``None`` when it is empty.

    ``width`` is the row length of the grid the cells were indexed on - it is not
    always :data:`GROUP_CELLS`, because the coarse pre-filter grid has a row
    length of its own.
    """
    if not cells:
        return None
    min_x = min(cell % width for cell in cells)
    max_x = max(cell % width for cell in cells)
    min_y = min(cell // width for cell in cells)
    max_y = max(cell // width for cell in cells)
    return (min_x, min_y, max_x, max_y)


def _boxes_close(left: Box, right: Box, gap: int) -> bool:
    """Whether two grid boxes come within ``gap`` cells of each other."""
    left_x0, left_y0, left_x1, left_y1 = left
    right_x0, right_y0, right_x1, right_y1 = right
    return (
        left_x0 - gap <= right_x1
        and right_x0 - gap <= left_x1
        and left_y0 - gap <= right_y1
        and right_y0 - gap <= left_y1
    )


def _box_within(inner: Box, outer: Box, slack: int = 1) -> bool:
    """Whether ``inner`` sits inside ``outer``, give or take a cell.

    A cheap necessary condition for one mask lying inside another - the case the
    outline distance cannot see - and a permissive one, so a pair that only
    nearly qualifies still reaches the exact test.
    """
    inner_x0, inner_y0, inner_x1, inner_y1 = inner
    outer_x0, outer_y0, outer_x1, outer_y1 = outer
    return (
        outer_x0 - slack <= inner_x0
        and outer_x1 + slack >= inner_x1
        and outer_y0 - slack <= inner_y0
        and outer_y1 + slack >= inner_y1
    )


def _cells_close(
    left: set[int],
    right: set[int],
    gap: int,
    width: int = GROUP_CELLS,
) -> bool:
    """Whether two cell sets come within ``gap`` cells of each other (max-norm).

    The smaller set is grown by ``gap`` cells and the larger one is looked up in
    it, so the work is proportional to the smaller mask's outline - the pair has
    already passed the bounding-box test by the time this runs. ``width`` is the
    row length of the grid the cells were indexed on: the coarse pre-filter grid
    has one of its own, and reading it back with the wrong one would answer about
    unrelated pairs.
    """
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    limit = width - 1

    for cell in smaller:
        x = cell % width
        y = cell // width
        for dx, dy in _OFFSETS[gap]:
            nx = x + dx
            ny = y + dy
            if nx < 0 or ny < 0 or nx > limit or ny > limit:
                continue
            if ny * width + nx in larger:
                return True
    return False


def _point_inside(rings: Sequence[Sequence[Point]], point: Point) -> bool:
    """Non-zero winding rule - the fill rule SVG uses, so the answer matches
    what the mask looks like on screen."""
    x, y = point
    winding = 0
    for ring in rings:
        count = len(ring)
        for index in range(count):
            ax, ay = ring[index - 1]
            bx, by = ring[index]
            # Half-open in y, so a vertex on the scanline is counted once.
            if (ay <= y < by) or (by <= y < ay):
                crossing = ax + (bx - ax) * (y - ay) / (by - ay)
                if crossing > x:
                    winding += 1 if by > ay else -1
    return winding != 0


def group_object_indices(entries: Sequence[Entry]) -> list[int]:
    """Assign every entry the index of the object it belongs to.

    ``entries`` is one frame's detections in the order they were produced, as
    ``(defect_class, mask)`` pairs. The return value is what is stored as
    ``defects.object_index``: equal numbers mean one object, and the numbers are
    handed out in the order the objects first appear, so the review list is
    ordered the way the detector saw the damage and stays stable across
    requests.
    """
    total = len(entries)
    if total == 0:
        return []

    outlines: list[set[int]] = []
    coarse: list[set[int]] = []
    coarse_boxes: list[Box | None] = []
    boxes: list[Box | None] = []
    masks: list[list[list[Point]]] = []
    samples: list[list[Point]] = []

    for _, mask in entries:
        rings = _as_rings(mask)
        masks.append(rings)
        points = _outline_points(rings)
        cells = {_cell_of(point) for point in points}
        outlines.append(cells)
        boxes.append(_box_of(cells))
        buckets = {_coarse_cell(cell) for cell in cells}
        coarse.append(buckets)
        coarse_boxes.append(_box_of(buckets, _COARSE_CELLS))
        if points:
            stride = max(1, len(points) // _CONTAINMENT_SAMPLES)
            samples.append(points[::stride])
        else:
            samples.append([])

    parent = list(range(total))

    def find(index: int) -> int:
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != root:  # Path compression, iteratively.
            parent[index], index = root, parent[index]
        return root

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for left in range(total):
        left_box = boxes[left]
        left_coarse_box = coarse_boxes[left]
        if left_box is None or left_coarse_box is None:
            continue
        for right in range(left + 1, total):
            # One object is one class: a crack over spalling stays two findings.
            if entries[left][0] != entries[right][0]:
                continue
            if find(left) == find(right):
                continue
            right_box = boxes[right]
            if right_box is None or not _boxes_close(left_box, right_box, MERGE_CELLS):
                continue
            # Two tiers, both of which can only say "no, they are further apart
            # than the gap": the coarse grid first (a few lookups per outline
            # cell), then the exact one on the outlines themselves. Skipping the
            # exact test is what keeps a frame with a hundred detections quick -
            # it is the shortlist of pairs that plausibly merge, not every pair,
            # that gets measured.
            if _cells_close(
                coarse[left], coarse[right], 1, _COARSE_CELLS
            ) and _cells_close(outlines[left], outlines[right], MERGE_CELLS):
                union(left, right)
                continue
            # An outline distance says nothing about a mask that lies *inside*
            # another: their edges are apart while the shapes overlap. Bounding
            # boxes screen for that case - only a pair where one box sits in the
            # other can contain - before the point-in-polygon test runs.
            right_coarse_box = coarse_boxes[right]
            if right_coarse_box is None or not (
                _box_within(left_coarse_box, right_coarse_box)
                or _box_within(right_coarse_box, left_coarse_box)
            ):
                continue
            contained = any(_point_inside(masks[right], point) for point in samples[left])
            if not contained:
                contained = any(_point_inside(masks[left], point) for point in samples[right])
            if contained:
                union(left, right)

    indices: list[int] = []
    assigned: dict[int, int] = {}
    for index in range(total):
        root = find(index)
        if root not in assigned:
            assigned[root] = len(assigned)
        indices.append(assigned[root])
    return indices


__all__ = ["MERGE_CELLS", "MERGE_DISTANCE", "GROUP_CELLS", "group_object_indices"]
