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


def extract_rebars(ifc: IFCIndex, type_axes: dict[int, TypeAxis]) -> list[Rebar]:
    out: list[Rebar] = []
    pcache: dict[int, np.ndarray] = {}
    product_types = ["IFCREINFORCINGBAR", "IFCBUILDINGELEMENTPROXY"]
    for product_type in product_types:
        for eid in ifc.by_type.get(product_type, []):
            f = ifc.fields(eid)
            name = token_string(f[2]) or ""
            objtype = token_string(f[4]) or ""
            tag = token_string(f[7]) or "" if len(f) > 7 else ""
            map_id, T = product_map_and_transform(ifc, eid, pcache)
            if map_id is None or map_id not in type_axes:
                continue
            ta = type_axes[map_id]
            dims = np.ptp(ta.axis, axis=0)
            slender = max(dims) / (2 * ta.radius + 1e-9)
            is_rebar = product_type == "IFCREINFORCINGBAR" or "钢筋" in name or "钢筋" in objtype or slender >= 5.0
            if not is_rebar:
                continue
            axis = transform_points(ta.axis, T)
            scale = float(np.mean(np.linalg.norm(T[:3, :3], axis=0)))
            r = ta.radius * scale
            bmin = axis.min(0) - r
            bmax = axis.max(0) + r
            length = float(np.linalg.norm(np.diff(axis, axis=0), axis=1).sum())
            guid = token_string(f[0]) or ""
            out.append(Rebar(len(out), eid, guid, name, tag, map_id, axis, r, bmin, bmax, length))
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
    rebars = extract_rebars(ifc, type_axes)
    if not rebars:
        raise RuntimeError("未识别到钢筋。请检查 IFC 导出类别、名称或几何表达。")
    meta = {
        "ifc_schema": "IFC2X3" if "IFC2X3" in path.read_text(encoding="latin1", errors="ignore")[:10000] else "unknown",
        "entity_count": len(ifc.entities),
        "representation_map_count": len(maps),
        "axis_type_count": len(type_axes),
        "map_failure_count": len(failures),
        "map_failures": failures,
    }
    cb("instantiate", 0.52, f"识别到 {len(rebars)} 根钢筋")
    return rebars, type_axes, meta
