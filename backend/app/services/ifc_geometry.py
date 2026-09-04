from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

ENTITY_RE = re.compile(r"#(\d+)=([A-Z0-9_]+)\((.*)\);\s*$")
REF_RE = re.compile(r"#(\d+)")
NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")
X2_RE = re.compile(r"\\X2\\([0-9A-Fa-f]+)\\X0\\")

Progress = Callable[[str, float, str], None]


def decode_ifc_text(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        try:
            return bytes.fromhex(m.group(1)).decode("utf-16-be")
        except Exception:
            return m.group(0)
    return X2_RE.sub(repl, text).replace("''", "'")


def split_top_level(s: str) -> list[str]:
    out: list[str] = []
    start = 0
    depth = 0
    in_str = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'":
            if in_str and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append(s[start:i].strip())
                start = i + 1
        i += 1
    out.append(s[start:].strip())
    return out


def refs(s: str) -> list[int]:
    return [int(x) for x in REF_RE.findall(s)]


def numbers(s: str) -> list[float]:
    return [float(x) for x in NUM_RE.findall(s)]


def token_ref(tok: str) -> Optional[int]:
    m = re.fullmatch(r"#(\d+)", tok.strip())
    return int(m.group(1)) if m else None


def token_string(tok: str) -> Optional[str]:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return decode_ifc_text(tok[1:-1])
    return None


class IFCIndex:
    """Compact STEP index suitable for large IFC2X3 files without IfcOpenShell."""

    def __init__(self, path: Path):
        self.path = path
        self.entities: dict[int, tuple[str, str]] = {}
        self.by_type: dict[str, list[int]] = defaultdict(list)
        with path.open("r", encoding="latin1", errors="ignore") as stream:
            for line in stream:
                m = ENTITY_RE.match(line)
                if m:
                    eid = int(m.group(1))
                    typ = m.group(2)
                    args = m.group(3)
                    self.entities[eid] = (typ, args)
                    self.by_type[typ].append(eid)

    def get(self, eid: int) -> tuple[str, str]:
        return self.entities[eid]

    def fields(self, eid: int) -> list[str]:
        return split_top_level(self.entities[eid][1])

    def point(self, eid: int) -> np.ndarray:
        vals = numbers(self.entities[eid][1])
        if len(vals) == 2:
            vals.append(0.0)
        return np.asarray(vals[:3], dtype=float)

    def direction(self, eid: Optional[int], default: Sequence[float]) -> np.ndarray:
        if eid is None:
            return np.asarray(default, dtype=float)
        v = self.point(eid)
        n = np.linalg.norm(v)
        return v / n if n > 1e-12 else np.asarray(default, dtype=float)


@dataclass
class TypeAxis:
    map_id: int
    axis: np.ndarray
    radius: float
    vertex_count: int
    face_count: int
    station_count: int
    method: str


@dataclass
class Rebar:
    index: int
    entity_id: int
    guid: str
    name: str
    tag: str
    map_id: int
    axis: np.ndarray
    radius: float
    bbox_min: np.ndarray
    bbox_max: np.ndarray
    length: float


def rebar_display_id(bar: Rebar) -> str:
    """Return a concise BIM-facing identifier for viewer and ordering UI."""
    name = str(bar.name or "").strip()
    parts = [part.strip() for part in name.split(":") if part.strip()]
    for part in reversed(parts):
        if part.isdigit() and len(part) >= 3:
            return part
    tag = str(bar.tag or "").strip()
    if tag.isdigit() and len(tag) >= 3:
        return tag
    return name or tag or bar.guid or str(bar.index)


def axis2placement_matrix(ifc: IFCIndex, eid: Optional[int]) -> np.ndarray:
    T = np.eye(4)
    if eid is None:
        return T
    typ, _ = ifc.get(eid)
    f = ifc.fields(eid)
    if typ == "IFCAXIS2PLACEMENT3D":
        loc = ifc.point(token_ref(f[0]))
        z = ifc.direction(token_ref(f[1]) if len(f) > 1 else None, (0, 0, 1))
        x0 = ifc.direction(token_ref(f[2]) if len(f) > 2 else None, (1, 0, 0))
        x = x0 - z * np.dot(x0, z)
        if np.linalg.norm(x) < 1e-10:
            x = np.array([1.0, 0.0, 0.0])
            x = x - z * np.dot(x, z)
        x /= max(np.linalg.norm(x), 1e-12)
        y = np.cross(z, x)
        y /= max(np.linalg.norm(y), 1e-12)
        T[:3, :3] = np.column_stack([x, y, z])
        T[:3, 3] = loc
    elif typ == "IFCAXIS2PLACEMENT2D":
        loc = ifc.point(token_ref(f[0]))
        x = ifc.direction(token_ref(f[1]) if len(f) > 1 else None, (1, 0, 0))
        x[2] = 0
        x /= max(np.linalg.norm(x), 1e-12)
        y = np.array([-x[1], x[0], 0.0])
        T[:3, :3] = np.column_stack([x, y, np.array([0.0, 0.0, 1.0])])
        T[:3, 3] = loc
    else:
        raise ValueError(f"Unsupported placement {typ} #{eid}")
    return T


def local_placement_matrix(ifc: IFCIndex, eid: Optional[int], cache: dict[int, np.ndarray]) -> np.ndarray:
    if eid is None:
        return np.eye(4)
    if eid in cache:
        return cache[eid]
    typ, _ = ifc.get(eid)
    if typ != "IFCLOCALPLACEMENT":
        return axis2placement_matrix(ifc, eid)
    f = ifc.fields(eid)
    parent = token_ref(f[0])
    rel = token_ref(f[1])
    T = local_placement_matrix(ifc, parent, cache) @ axis2placement_matrix(ifc, rel)
    cache[eid] = T
    return T


def cartesian_transform_matrix(ifc: IFCIndex, eid: Optional[int]) -> np.ndarray:
    if eid is None:
        return np.eye(4)
    typ, _ = ifc.get(eid)
    if typ not in ("IFCCARTESIANTRANSFORMATIONOPERATOR3D", "IFCCARTESIANTRANSFORMATIONOPERATOR3DNONUNIFORM"):
        return np.eye(4)
    f = ifc.fields(eid)
    a1 = ifc.direction(token_ref(f[0]), (1, 0, 0))
    a2 = ifc.direction(token_ref(f[1]), (0, 1, 0))
    org = ifc.point(token_ref(f[2]))
    scale = 1.0 if f[3] in ("$", "*") else float(f[3])
    a3 = ifc.direction(token_ref(f[4]) if len(f) > 4 else None, np.cross(a1, a2))
    x = a1 / max(np.linalg.norm(a1), 1e-12)
    z = a3 / max(np.linalg.norm(a3), 1e-12)
    y = a2 - x * np.dot(a2, x) - z * np.dot(a2, z)
    if np.linalg.norm(y) < 1e-12:
        y = np.cross(z, x)
    y /= max(np.linalg.norm(y), 1e-12)
    T = np.eye(4)
    T[:3, :3] = np.column_stack([x, y, z]) * scale
    T[:3, 3] = org
    return T


def transform_points(P: np.ndarray, T: np.ndarray) -> np.ndarray:
    return P @ T[:3, :3].T + T[:3, 3]


def find_brep_for_map(ifc: IFCIndex, map_id: int) -> Optional[int]:
    f = ifc.fields(map_id)
    rep_id = token_ref(f[1])
    if rep_id is None:
        return None
    for rid in refs(ifc.get(rep_id)[1]):
        if rid == token_ref(ifc.fields(rep_id)[0]):
            continue
        typ, _ = ifc.get(rid)
        if typ == "IFCFACETEDBREP":
            return rid
    return None


def mesh_from_brep(ifc: IFCIndex, brep_id: int) -> tuple[np.ndarray, list[list[int]], list[int]]:
    shell_id = refs(ifc.get(brep_id)[1])[0]
    face_ids = refs(ifc.get(shell_id)[1])
    loop_point_ids: list[list[int]] = []
    all_pt_ids: list[int] = []
    for face_id in face_ids:
        for bound_id in refs(ifc.get(face_id)[1]):
            br = refs(ifc.get(bound_id)[1])
            if not br:
                continue
            loop_id = br[0]
            ids = refs(ifc.get(loop_id)[1])
            if len(ids) >= 3:
                loop_point_ids.append(ids)
                all_pt_ids.extend(ids)
    unique = sorted(set(all_pt_ids))
    idx = {pid: i for i, pid in enumerate(unique)}
    V = np.asarray([ifc.point(pid) for pid in unique], dtype=float)
    loops = [[idx[p] for p in ids] for ids in loop_point_ids]
    return V, loops, face_ids


def simplify_polyline(P: np.ndarray, tol: float) -> np.ndarray:
    if len(P) <= 2 or tol <= 0:
        return P
    keep = np.zeros(len(P), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(P) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        A = P[a]
        B = P[b]
        AB = B - A
        den = float(AB @ AB)
        Q = P[a + 1:b]
        if den < 1e-18:
            d = np.linalg.norm(Q - A, axis=1)
        else:
            t = np.clip(((Q - A) @ AB) / den, 0, 1)
            d = np.linalg.norm(Q - (A + t[:, None] * AB), axis=1)
        k = int(np.argmax(d))
        if float(d[k]) > tol:
            i = a + 1 + k
            keep[i] = True
            stack.extend([(a, i), (i, b)])
    return P[keep]


def extract_axis_from_brep(ifc: IFCIndex, map_id: int, simplify_mm: float = 0.75) -> TypeAxis:
    brep = find_brep_for_map(ifc, map_id)
    if brep is None:
        raise ValueError(f"No faceted BREP for map #{map_id}")
    V, loops, face_ids = mesh_from_brep(ifc, brep)
    if len(V) < 4:
        raise ValueError("Too few vertices")
    max_loop = max(len(x) for x in loops)
    cap_candidates = [x for x in loops if len(x) == max_loop and len(x) >= 6]
    if len(cap_candidates) >= 2:
        centroids = np.asarray([V[x].mean(axis=0) for x in cap_candidates])
        D = np.linalg.norm(centroids[:, None, :] - centroids[None, :, :], axis=2)
        a, b = np.unravel_index(np.argmax(D), D.shape)
        cap0, cap1 = cap_candidates[a], cap_candidates[b]
        q = max_loop
    else:
        mu = V.mean(0)
        _, _, vh = np.linalg.svd(V - mu, full_matrices=False)
        u = vh[0]
        s = (V - mu) @ u
        q = min(24, max(6, len(V) // 4))
        cap0 = np.argsort(s)[:q].tolist()
        cap1 = np.argsort(s)[-q:].tolist()
    c0 = V[cap0].mean(axis=0)
    c1 = V[cap1].mean(axis=0)
    r0 = np.median(np.linalg.norm(V[cap0] - c0, axis=1))
    r1 = np.median(np.linalg.norm(V[cap1] - c1, axis=1))
    radius = float(max(0.1, 0.5 * (r0 + r1)))

    edges: set[tuple[int, int]] = set()
    for ids in loops:
        for u, v in zip(ids, ids[1:] + ids[:1]):
            if u != v:
                edges.add((u, v) if u < v else (v, u))
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    for u, v in edges:
        w = float(np.linalg.norm(V[u] - V[v]))
        rows += [u, v]
        cols += [v, u]
        data += [w, w]
    A = coo_matrix((data, (rows, cols)), shape=(len(V), len(V))).tocsr()
    d0 = np.min(dijkstra(A, directed=False, indices=np.asarray(cap0)), axis=0)
    d1 = np.min(dijkstra(A, directed=False, indices=np.asarray(cap1)), axis=0)
    if not np.all(np.isfinite(d0)) or not np.all(np.isfinite(d1)):
        raise ValueError("Disconnected BREP surface")
    t = d0 / (d0 + d1 + 1e-12)
    K = max(2, int(round(len(V) / q)))
    order = np.argsort(t)
    groups = np.array_split(order, K)
    C = np.asarray([V[g].mean(axis=0) for g in groups])
    C[0] = c0
    C[-1] = c1
    if np.linalg.norm(C[0] - c0) > np.linalg.norm(C[-1] - c0):
        C = C[::-1]
    if len(C) > 4:
        S = C.copy()
        S[1:-1] = 0.25 * C[:-2] + 0.5 * C[1:-1] + 0.25 * C[2:]
        C = S
    C = simplify_polyline(C, tol=max(simplify_mm, 0.08 * radius))
    return TypeAxis(map_id, C, radius, len(V), len(face_ids), K, "cap-geodesic-centroid")



def find_representation_item(ifc: IFCIndex, map_id: int, accepted: set[str]) -> Optional[int]:
    f = ifc.fields(map_id)
    rep_id = token_ref(f[1])
    if rep_id is None:
        return None
    rep_fields = ifc.fields(rep_id)
    item_refs = refs(rep_fields[3]) if len(rep_fields) > 3 else refs(ifc.get(rep_id)[1])
    for rid in item_refs:
        if rid in ifc.entities and ifc.get(rid)[0] in accepted:
            return rid
    return None


def extract_axis_from_extruded_solid(ifc: IFCIndex, map_id: int, solid_id: int) -> TypeAxis:
    fields = ifc.fields(solid_id)
    profile_id = token_ref(fields[0])
    position_id = token_ref(fields[1])
    direction_id = token_ref(fields[2])
    depth = float(fields[3])
    if profile_id is None or direction_id is None:
        raise ValueError(f"Invalid IfcExtrudedAreaSolid #{solid_id}")
    ptyp, _ = ifc.get(profile_id)
    pf = ifc.fields(profile_id)
    if ptyp not in ("IFCCIRCLEPROFILEDEF", "IFCCIRCLEHOLLOWPROFILEDEF"):
        raise ValueError(f"Unsupported extrusion profile {ptyp}")
    radius = float(pf[3])
    profile_pos_id = token_ref(pf[2])
    profile_T = axis2placement_matrix(ifc, profile_pos_id) if profile_pos_id is not None else np.eye(4)
    center = profile_T[:3, 3]
    d = ifc.direction(direction_id, (0, 0, 1))
    local = np.vstack([center, center + d * depth])
    solid_T = axis2placement_matrix(ifc, position_id) if position_id is not None else np.eye(4)
    axis = transform_points(local, solid_T)
    return TypeAxis(map_id, axis, radius, 0, 0, 2, "extruded-circle-axis")



def _trim_value(ifc: IFCIndex, token: str) -> tuple[str, object]:
    """Read an IfcTrimmedCurve trim as either a point or a parameter."""
    ref = token_ref(token)
    if ref is not None and ref in ifc.entities:
        return "point", ifc.point(ref)
    values = numbers(token)
    return "parameter", (values[0] if values else None)


def _curve_parameter(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def _circle_parameter_radians(value: float) -> float:
    # V1.ifc stores circle trims as degrees (for example 270 -> 360), while
    # some exporters write radians. The range is enough to distinguish them.
    return math.radians(value) if abs(value) > (2.0 * math.pi + 1e-6) else value


def _circle_sweep_delta(start: float, end: float, sense_agreement: bool) -> float:
    """Return the directed circle sweep while preserving IFC curve sense."""
    turn = 2.0 * math.pi
    raw = end - start
    if sense_agreement:
        delta = raw % turn
        if abs(delta) < 1e-12 and abs(raw) > 1e-12:
            delta = turn
    else:
        delta = -((-raw) % turn)
        if abs(delta) < 1e-12 and abs(raw) > 1e-12:
            delta = -turn
    return delta


def _curve_points(ifc: IFCIndex, curve_id: int, cache: dict[int, np.ndarray]) -> np.ndarray:
    """Sample the common IFC curve entities used by swept-disk rebar."""
    if curve_id in cache:
        return cache[curve_id].copy()
    typ, _ = ifc.get(curve_id)
    fields = ifc.fields(curve_id)

    if typ == "IFCPOLYLINE":
        point_ids = refs(fields[0]) if fields else []
        points = np.asarray([ifc.point(pid) for pid in point_ids], dtype=float)
    elif typ == "IFCCOMPOSITECURVE":
        points_list: list[np.ndarray] = []
        for segment_id in refs(fields[0]) if fields else []:
            segment_fields = ifc.fields(segment_id)
            same_sense = len(segment_fields) < 2 or segment_fields[1] == ".T."
            parent_id = token_ref(segment_fields[2]) if len(segment_fields) > 2 else None
            if parent_id is None:
                continue
            part = _curve_points(ifc, parent_id, cache)
            if not same_sense:
                part = part[::-1]
            if len(part) == 0:
                continue
            if points_list:
                previous = points_list[-1][-1]
                if np.linalg.norm(previous - part[-1]) < 1e-6 and np.linalg.norm(previous - part[0]) >= 1e-6:
                    part = part[::-1]
                if np.linalg.norm(previous - part[0]) < 1e-6:
                    part = part[1:]
            if len(part):
                points_list.append(part)
        points = np.vstack(points_list) if points_list else np.empty((0, 3), dtype=float)
    elif typ == "IFCCOMPOSITECURVESEGMENT":
        parent_id = token_ref(fields[2]) if len(fields) > 2 else None
        points = _curve_points(ifc, parent_id, cache) if parent_id is not None else np.empty((0, 3), dtype=float)
        if len(fields) > 1 and fields[1] == ".F.":
            points = points[::-1]
    elif typ == "IFCTRIMMEDCURVE":
        basis_id = token_ref(fields[0]) if fields else None
        if basis_id is None or basis_id not in ifc.entities:
            points = np.empty((0, 3), dtype=float)
        else:
            basis_typ, _ = ifc.get(basis_id)
            trim1_kind, trim1 = _trim_value(ifc, fields[1] if len(fields) > 1 else "$")
            trim2_kind, trim2 = _trim_value(ifc, fields[2] if len(fields) > 2 else "$")
            sense = len(fields) < 4 or fields[3] == ".T."
            if basis_typ == "IFCCIRCLE":
                bf = ifc.fields(basis_id)
                placement_id = token_ref(bf[0])
                placement = axis2placement_matrix(ifc, placement_id)
                radius = float(bf[1])
                if trim1_kind == "point":
                    v = np.asarray(trim1) - placement[:3, 3]
                    trim1 = math.atan2(float(v @ placement[:3, 1]), float(v @ placement[:3, 0]))
                if trim2_kind == "point":
                    v = np.asarray(trim2) - placement[:3, 3]
                    trim2 = math.atan2(float(v @ placement[:3, 1]), float(v @ placement[:3, 0]))
                start = _circle_parameter_radians(_curve_parameter(trim1))
                end = _circle_parameter_radians(_curve_parameter(trim2, start))
                delta = _circle_sweep_delta(start, end, sense)
                count = max(3, int(math.ceil(abs(delta) / (math.pi / 18.0))) + 1)
                params = np.linspace(start, start + delta, count)
                points = placement[:3, 3] + radius * (
                    np.cos(params)[:, None] * placement[:3, 0]
                    + np.sin(params)[:, None] * placement[:3, 1]
                )
            elif basis_typ == "IFCLINE":
                bf = ifc.fields(basis_id)
                origin = ifc.point(token_ref(bf[0]))
                direction = ifc.direction(token_ref(bf[1]), (1, 0, 0))
                if trim1_kind == "point":
                    p0 = np.asarray(trim1, dtype=float)
                    t0 = float((p0 - origin) @ direction)
                else:
                    t0 = _curve_parameter(trim1)
                    p0 = origin + direction * t0
                if trim2_kind == "point":
                    p1 = np.asarray(trim2, dtype=float)
                    t1 = float((p1 - origin) @ direction)
                else:
                    t1 = _curve_parameter(trim2, t0 + 1.0)
                    p1 = origin + direction * t1
                points = np.asarray([p0, p1], dtype=float)
            else:
                points = np.empty((0, 3), dtype=float)
            if not sense:
                points = points[::-1]
    elif typ == "IFCLINE":
        origin = ifc.point(token_ref(fields[0]))
        direction = ifc.direction(token_ref(fields[1]), (1, 0, 0))
        points = np.asarray([origin, origin + direction], dtype=float)
    else:
        points = np.empty((0, 3), dtype=float)

    cache[curve_id] = points.copy()
    return points


def extract_axis_from_swept_disk(
    ifc: IFCIndex,
    item_id: int,
    simplify_mm: float = 0.75,
    curve_cache: Optional[dict[int, np.ndarray]] = None,
) -> TypeAxis:
    fields = ifc.fields(item_id)
    directrix_id = token_ref(fields[0]) if fields else None
    if directrix_id is None:
        raise ValueError(f"Invalid swept disk #{item_id}")
    radius = float(fields[1]) if len(fields) > 1 and fields[1] not in ("$", "*") else 1.0
    axis = _curve_points(ifc, directrix_id, curve_cache if curve_cache is not None else {})
    if len(axis) < 2:
        raise ValueError(f"Swept disk #{item_id} has no usable directrix")
    axis = simplify_polyline(axis, simplify_mm)
    return TypeAxis(item_id, axis, max(0.1, radius), 0, 0, len(axis), "swept-disk-directrix")


def extract_axis_from_shape_item(
    ifc: IFCIndex,
    item_id: int,
    simplify_mm: float = 0.75,
    curve_cache: Optional[dict[int, np.ndarray]] = None,
) -> TypeAxis:
    typ, _ = ifc.get(item_id)
    if typ in {"IFCSWEPTDISKSOLID", "IFCSWEPTDISKSOLIDTAPERED"}:
        return extract_axis_from_swept_disk(ifc, item_id, simplify_mm, curve_cache)
    if typ == "IFCFACETEDBREP":
        V, loops, face_ids = mesh_from_brep(ifc, item_id)
        if len(V) < 4:
            raise ValueError(f"Too few vertices in BREP #{item_id}")
        max_loop = max(len(x) for x in loops)
        cap_candidates = [x for x in loops if len(x) == max_loop and len(x) >= 6]
        if len(cap_candidates) >= 2:
            centroids = np.asarray([V[x].mean(axis=0) for x in cap_candidates])
            D = np.linalg.norm(centroids[:, None, :] - centroids[None, :, :], axis=2)
            a, b = np.unravel_index(np.argmax(D), D.shape)
            c0, c1 = centroids[a], centroids[b]
            cap0, cap1 = cap_candidates[a], cap_candidates[b]
        else:
            mu = V.mean(0)
            _, _, vh = np.linalg.svd(V - mu, full_matrices=False)
            u = vh[0]
            s = (V - mu) @ u
            q = min(24, max(6, len(V) // 4))
            cap0 = np.argsort(s)[:q].tolist()
            cap1 = np.argsort(s)[-q:].tolist()
            c0, c1 = V[cap0].mean(0), V[cap1].mean(0)
        radius = float(max(0.1, 0.5 * (
            np.median(np.linalg.norm(V[cap0] - c0, axis=1))
            + np.median(np.linalg.norm(V[cap1] - c1, axis=1))
        )))
        axis = simplify_polyline(np.asarray([c0, c1]), simplify_mm)
        return TypeAxis(item_id, axis, radius, len(V), len(face_ids), len(axis), "direct-brep-centroid")
    if typ == "IFCEXTRUDEDAREASOLID":
        return extract_axis_from_extruded_solid(ifc, item_id, item_id)
    if typ in {"IFCPOLYLINE", "IFCCOMPOSITECURVE", "IFCTRIMMEDCURVE"}:
        axis = _curve_points(ifc, item_id, curve_cache if curve_cache is not None else {})
        if len(axis) < 2:
            raise ValueError(f"Curve #{item_id} has no usable points")
        return TypeAxis(item_id, simplify_polyline(axis, simplify_mm), 1.0, 0, 0, len(axis), "direct-curve")
    raise ValueError(f"Unsupported direct shape item {typ} #{item_id}")

def extract_axis_from_map(ifc: IFCIndex, map_id: int, simplify_mm: float = 0.75) -> TypeAxis:
    brep = find_representation_item(ifc, map_id, {"IFCFACETEDBREP"})
    if brep is not None:
        return extract_axis_from_brep(ifc, map_id, simplify_mm=simplify_mm)
    solid = find_representation_item(ifc, map_id, {"IFCEXTRUDEDAREASOLID"})
    if solid is not None:
        return extract_axis_from_extruded_solid(ifc, map_id, solid)
    raise ValueError(f"No supported body geometry for map #{map_id}")

def product_map_and_transform(ifc: IFCIndex, product_id: int, placement_cache: dict[int, np.ndarray]) -> tuple[Optional[int], np.ndarray]:
    f = ifc.fields(product_id)
    placement_id = token_ref(f[5]) if len(f) > 5 else None
    pds_id = token_ref(f[6]) if len(f) > 6 else None
    Tprod = local_placement_matrix(ifc, placement_id, placement_cache)
    if pds_id is None:
        return None, Tprod
    for rep_id in refs(ifc.get(pds_id)[1]):
        typ, _ = ifc.get(rep_id)
        if typ != "IFCSHAPEREPRESENTATION":
            continue
        rep_fields = ifc.fields(rep_id)
        item_refs = refs(rep_fields[3]) if len(rep_fields) > 3 else refs(ifc.get(rep_id)[1])[1:]
        for item_id in item_refs:
            typ, _ = ifc.get(item_id)
            if typ == "IFCMAPPEDITEM":
                mf = ifc.fields(item_id)
                map_id = token_ref(mf[0])
                target_id = token_ref(mf[1])
                if map_id is None:
                    continue
                map_origin_id = token_ref(ifc.fields(map_id)[0])
                Tmap = cartesian_transform_matrix(ifc, target_id) @ np.linalg.inv(axis2placement_matrix(ifc, map_origin_id))
                return map_id, Tprod @ Tmap
    return None, Tprod


def product_shape_items(
    ifc: IFCIndex,
    product_id: int,
    placement_cache: dict[int, np.ndarray],
) -> tuple[list[tuple[int, np.ndarray]], np.ndarray]:
    """Return direct shape items and the product placement transform."""
    f = ifc.fields(product_id)
    placement_id = token_ref(f[5]) if len(f) > 5 else None
    pds_id = token_ref(f[6]) if len(f) > 6 else None
    Tprod = local_placement_matrix(ifc, placement_id, placement_cache)
    if pds_id is None or pds_id not in ifc.entities:
        return [], Tprod
    items: list[tuple[int, np.ndarray]] = []
    for rep_id in refs(ifc.get(pds_id)[1]):
        if rep_id not in ifc.entities or ifc.get(rep_id)[0] != "IFCSHAPEREPRESENTATION":
            continue
        rep_fields = ifc.fields(rep_id)
        for item_id in refs(rep_fields[3]) if len(rep_fields) > 3 else []:
            if item_id in ifc.entities and ifc.get(item_id)[0] != "IFCMAPPEDITEM":
                items.append((item_id, Tprod))
    return items, Tprod


def _product_radius_hint(fields: list[str]) -> Optional[float]:
    # IfcReinforcingBar.NominalDiameter is attribute 9 in IFC2X3.
    if len(fields) <= 9 or fields[9] in ("$", "*"):
        return None
    values = numbers(fields[9])
    return float(values[0]) * 0.5 if values and values[0] > 0 else None


def extract_rebars(ifc: IFCIndex, type_axes: dict[int, TypeAxis], simplify_mm: float = 0.75) -> list[Rebar]:
    out: list[Rebar] = []
    pcache: dict[int, np.ndarray] = {}
    curve_cache: dict[int, np.ndarray] = {}
    product_types = [
        "IFCREINFORCINGBAR",
        "IFCTENDON",
        "IFCTENDONANCHOR",
        "IFCBUILDINGELEMENTPROXY",
    ]
    for product_type in product_types:
        for eid in ifc.by_type.get(product_type, []):
            f = ifc.fields(eid)
            name = token_string(f[2]) or "" if len(f) > 2 else ""
            objtype = token_string(f[4]) or "" if len(f) > 4 else ""
            tag = token_string(f[7]) or "" if len(f) > 7 else ""
            map_id, Tmap = product_map_and_transform(ifc, eid, pcache)
            candidates: list[tuple[TypeAxis, np.ndarray]] = []
            if map_id is not None and map_id in type_axes:
                candidates.append((type_axes[map_id], Tmap))
            else:
                direct_items, Tprod = product_shape_items(ifc, eid, pcache)
                for item_id, _ in direct_items:
                    try:
                        ta = extract_axis_from_shape_item(
                            ifc, item_id, simplify_mm=simplify_mm, curve_cache=curve_cache
                        )
                    except Exception:
                        continue
                    candidates.append((ta, Tprod))
            for ta, T in candidates[:1]:
                dims = np.ptp(ta.axis, axis=0)
                hint = _product_radius_hint(f)
                radius = hint if ta.method == "direct-curve" and hint is not None else ta.radius
                slender = max(dims) / (2 * radius + 1e-9)
                text = f"{name} {objtype}".casefold()
                is_named_rebar = any(
                    key in text for key in ("rebar", "reinforc", "tendon", "prestress", "pre-stress", "strand", "\u94a2\u7b4b", "\u9884\u5e94\u529b")
                )
                is_rebar = product_type in {"IFCREINFORCINGBAR", "IFCTENDON", "IFCTENDONANCHOR"} or is_named_rebar or slender >= 5.0
                if not is_rebar:
                    continue
                axis = transform_points(ta.axis, T)
                scale = float(np.mean(np.linalg.norm(T[:3, :3], axis=0)))
                r = radius * scale
                bmin = axis.min(0) - r
                bmax = axis.max(0) + r
                length = float(np.linalg.norm(np.diff(axis, axis=0), axis=1).sum())
                guid = token_string(f[0]) or ""
                out.append(Rebar(len(out), eid, guid, name, tag, ta.map_id, axis, r, bmin, bmax, length))
    return out


def parse_ifc_rebars(path: Path, simplify_mm: float = 0.75, progress: Optional[Progress] = None) -> tuple[list[Rebar], dict[int, TypeAxis], dict]:
    cb = progress or (lambda *_: None)
    cb("parse", 0.04, "建立 IFC STEP 实体索引")
    ifc = IFCIndex(path)
    maps = ifc.by_type.get("IFCREPRESENTATIONMAP", [])
    cb("axis", 0.10, f"恢复 {len(maps)} 类映射几何的中心轴")
    type_axes: dict[int, TypeAxis] = {}
    failures: dict[int, str] = {}
    for k, map_id in enumerate(maps, 1):
        try:
            type_axes[map_id] = extract_axis_from_map(ifc, map_id, simplify_mm=simplify_mm)
        except Exception as exc:
            failures[map_id] = str(exc)
        if k % max(1, len(maps) // 20) == 0 or k == len(maps):
            cb("axis", 0.10 + 0.34 * k / max(1, len(maps)), f"已恢复 {k}/{len(maps)} 类几何")
    cb("instantiate", 0.46, "实例化钢筋轴线并应用 IFC 坐标变换")
    rebars = extract_rebars(ifc, type_axes, simplify_mm=simplify_mm)
    direct_geometry_count = sum(1 for bar in rebars if bar.map_id not in type_axes)
    if not rebars:
        raise RuntimeError("未识别到钢筋。请检查 IFC 导出类别、名称或几何表达。")
    meta = {
        "ifc_schema": "IFC2X3" if "IFC2X3" in path.read_text(encoding="latin1", errors="ignore")[:10000] else "unknown",
        "entity_count": len(ifc.entities),
        "representation_map_count": len(maps),
        "axis_type_count": len(type_axes),
        "map_failure_count": len(failures),
        "map_failures": failures,
        "direct_geometry_product_count": direct_geometry_count,
    }
    cb("instantiate", 0.52, f"识别到 {len(rebars)} 根钢筋")
    return rebars, type_axes, meta


def parse_ifc_rebar_models(
    models: Sequence[tuple[Path, str]],
    simplify_mm: float = 0.75,
    progress: Optional[Progress] = None,
) -> tuple[list[Rebar], dict[int, TypeAxis], dict]:
    """Parse one or more IFC files into one index-stable rebar model.

    Every input file keeps its original world coordinates. Rebar indices and
    representation-map ids are made unique across files, while BIM-facing
    names, tags, GUIDs and entity ids remain unchanged. ``source_models`` in
    the returned metadata records the exact bar indices belonging to each IFC.
    """
    if not models:
        raise ValueError("至少需要一个 IFC 模型")
    cb = progress or (lambda *_: None)
    if len(models) == 1:
        path, source_filename = models[0]
        rebars, type_axes, meta = parse_ifc_rebars(
            path,
            simplify_mm=simplify_mm,
            progress=cb,
        )
        return rebars, type_axes, {
            **meta,
            "source_file_count": 1,
            "source_models": [{
                "source_filename": source_filename,
                "rebar_count": len(rebars),
                "bar_indices": [bar.index for bar in rebars],
                "ifc_schema": meta.get("ifc_schema", "unknown"),
            }],
        }
    combined_rebars: list[Rebar] = []
    combined_type_axes: dict[int, TypeAxis] = {}
    source_models: list[dict] = []
    metas: list[dict] = []

    for file_position, (path, source_filename) in enumerate(models, 1):
        def file_progress(stage: str, value: float, message: str) -> None:
            fraction = ((file_position - 1) + min(max(float(value), 0.0), 0.52) / 0.52) / len(models)
            cb(stage, min(0.52, 0.52 * fraction), f"[{file_position}/{len(models)}] {message}")

        bars, type_axes, meta = parse_ifc_rebars(
            path,
            simplify_mm=simplify_mm,
            progress=file_progress,
        )
        metas.append(meta)
        map_ids = sorted({*type_axes, *(bar.map_id for bar in bars)})
        map_id_lookup = {
            old_id: file_position * 1_000_000_000 + ordinal
            for ordinal, old_id in enumerate(map_ids, 1)
        }
        for old_id, type_axis in type_axes.items():
            new_id = map_id_lookup[old_id]
            combined_type_axes[new_id] = TypeAxis(
                map_id=new_id,
                axis=np.asarray(type_axis.axis, dtype=float),
                radius=float(type_axis.radius),
                vertex_count=int(type_axis.vertex_count),
                face_count=int(type_axis.face_count),
                station_count=int(type_axis.station_count),
                method=str(type_axis.method),
            )

        indices: list[int] = []
        for bar in bars:
            new_index = len(combined_rebars)
            indices.append(new_index)
            combined_rebars.append(Rebar(
                index=new_index,
                entity_id=int(bar.entity_id),
                guid=bar.guid,
                name=bar.name,
                tag=bar.tag,
                map_id=map_id_lookup.get(bar.map_id, file_position * 1_000_000_000),
                axis=np.asarray(bar.axis, dtype=float),
                radius=float(bar.radius),
                bbox_min=np.asarray(bar.bbox_min, dtype=float),
                bbox_max=np.asarray(bar.bbox_max, dtype=float),
                length=float(bar.length),
            ))
        source_models.append({
            "source_filename": source_filename,
            "rebar_count": len(indices),
            "bar_indices": indices,
            "ifc_schema": meta.get("ifc_schema", "unknown"),
        })

    schemas = sorted({str(meta.get("ifc_schema", "unknown")) for meta in metas})
    failures = {
        f"{position}:{map_id}": message
        for position, meta in enumerate(metas, 1)
        for map_id, message in (meta.get("map_failures") or {}).items()
    }
    combined_meta = {
        "ifc_schema": schemas[0] if len(schemas) == 1 else "+".join(schemas),
        "entity_count": sum(int(meta.get("entity_count", 0)) for meta in metas),
        "representation_map_count": sum(int(meta.get("representation_map_count", 0)) for meta in metas),
        "axis_type_count": len(combined_type_axes),
        "map_failure_count": len(failures),
        "map_failures": failures,
        "direct_geometry_product_count": sum(
            int(meta.get("direct_geometry_product_count", 0)) for meta in metas
        ),
        "source_file_count": len(source_models),
        "source_models": source_models,
    }
    cb("instantiate", 0.52, f"已合并 {len(models)} 个 IFC，共识别到 {len(combined_rebars)} 根钢筋")
    return combined_rebars, combined_type_axes, combined_meta
