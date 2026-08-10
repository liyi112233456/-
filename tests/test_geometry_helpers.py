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



def test_parse_direct_swept_disk_rebar(tmp_path):
    from app.services.ifc_geometry import parse_ifc_rebars

    # This is the direct geometry pattern used by V1.ifc:
    # IfcReinforcingBar -> IfcProductDefinitionShape -> IfcSweptDiskSolid
    # -> IfcCompositeCurve -> IfcPolyline. No IfcRepresentationMap is used.
    content = """ISO-10303-21;
HEADER;
FILE_SCHEMA(('IFC2X3'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCCARTESIANPOINT((100.,0.,0.));
#3=IFCPOLYLINE((#1,#2));
#4=IFCCOMPOSITECURVESEGMENT(.CONTINUOUS.,.T.,#3);
#5=IFCCOMPOSITECURVE((#4),.F.);
#6=IFCSWEPTDISKSOLID(#5,6.,$,0.,1.);
#7=IFCSHAPEREPRESENTATION($,'Body','AdvancedSweptSolid',(#6));
#8=IFCPRODUCTDEFINITIONSHAPE($,$,(#7));
#9=IFCREINFORCINGBAR('guid',$,'direct-rebar',$,$,$,#8,'R-001',12.,113.,100.,.NOTDEFINED.,$);
ENDSEC;
END-ISO-10303-21;
"""
    path = tmp_path / "direct.ifc"
    path.write_text(content, encoding="latin1")
    bars, _, meta = parse_ifc_rebars(path)
    assert len(bars) == 1
    assert meta["direct_geometry_product_count"] == 1
    assert bars[0].tag == "R-001"
    assert bars[0].radius == 6.0
    assert np.allclose(bars[0].axis[[0, -1]], [[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]])



def test_sample_trimmed_circle_curve(tmp_path):
    from app.services.ifc_geometry import IFCIndex, _curve_points

    content = """ISO-10303-21;
HEADER;
FILE_SCHEMA(('IFC2X3'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCDIRECTION((0.,0.,1.));
#3=IFCDIRECTION((1.,0.,0.));
#4=IFCAXIS2PLACEMENT3D(#1,#2,#3);
#5=IFCCIRCLE(#4,10.);
#6=IFCTRIMMEDCURVE(#5,(IFCPARAMETERVALUE(0.)),(IFCPARAMETERVALUE(90.)),.T.,.PARAMETER.);
ENDSEC;
END-ISO-10303-21;
"""
    path = tmp_path / "arc.ifc"
    path.write_text(content, encoding="latin1")
    index = IFCIndex(path)
    points = _curve_points(index, 6, {})
    assert len(points) >= 4
    assert np.allclose(points[0], [10.0, 0.0, 0.0])
    assert np.allclose(points[-1], [0.0, 10.0, 0.0], atol=1e-6)
