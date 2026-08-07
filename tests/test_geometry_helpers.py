import numpy as np
from app.services.ifc_geometry import simplify_polyline
from app.services.planner import _closest_params_2d_batch


def test_simplify_straight_line():
    p=np.array([[0.,0,0],[1,0,0],[2,0,0]])
    q=simplify_polyline(p,.01)
    assert len(q)==2


def test_segment_distance_crossing():
    p0=np.array([[0.,0.]]);p1=np.array([[1.,1.]])
    q0=np.array([[0.,1.]]);q1=np.array([[1.,0.]])
    d,t,u=_closest_params_2d_batch(p0,p1,q0,q1)
    assert d[0] < 1e-8
    assert abs(t[0]-.5)<1e-6
    assert abs(u[0]-.5)<1e-6
