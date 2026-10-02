"""
AMR Simulator DXF Scene
"""

import math
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen, QBrush, QFont
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
)

try:
    import ezdxf
except Exception:  # pragma: no cover
    ezdxf = None


class DXFScene:
    def __init__(self):
        self.path = None
        self.entities: List[Dict] = []
        self.bounds: Optional[Tuple[float, float, float, float]] = None

    def clear(self):
        self.path = None
        self.entities = []
        self.bounds = None

    def set_content(
        self,
        path: Optional[str],
        entities: Optional[List[Dict]],
        bounds: Optional[Tuple[float, float, float, float]],
    ):
        self.path = path
        self.entities = list(entities or [])
        self.bounds = bounds

    @classmethod
    def from_content(
        cls,
        path: Optional[str],
        entities: Optional[List[Dict]],
        bounds: Optional[Tuple[float, float, float, float]],
    ):
        scene = cls()
        scene.set_content(path, entities, bounds)
        return scene

    @staticmethod
    def _bbox_from_points(points):
        if not points:
            return None
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        return (min(xs), min(ys), max(xs), max(ys))

    DXF_UNIT_TO_METRES = {
        0: None,  # Unitless
        1: 0.0254,  # Inches
        2: 0.3048,  # Feet
        4: 0.001,  # Millimetres
        5: 0.01,  # Centimetres
        6: 1.0,  # Metres
        7: 1000.0,  # Kilometres
        10: 0.9144,  # Yards
        14: 0.1,  # Decimetres
        21: 1200.0 / 3937.0,  # US survey feet
    }

    @classmethod
    def _detect_dxf_unit_scale(cls, doc) -> float:
        """
        Return a multiplier that converts DXF drawing units to metres.

        Uses $INSUNITS when available.
        If the drawing is unitless, assume metres to avoid unexpectedly
        shrinking already-correct AMR layouts.
        """
        try:
            insunits = int(doc.header.get("$INSUNITS", 0) or 0)
        except Exception:
            insunits = 0

        scale = cls.DXF_UNIT_TO_METRES.get(insunits)

        if scale is None:
            return 1.0

        return float(scale)

    @classmethod
    def load_content(cls, path: str):
        if ezdxf is None:
            raise RuntimeError(
                "ezdxf is not installed. Install with: pip install ezdxf"
            )

        doc = ezdxf.readfile(path)
        unit_scale = cls._detect_dxf_unit_scale(doc)

        msp = doc.modelspace()
        entities: List[Dict] = []
        all_points = []

        def scale_value(value):
            return float(value) * unit_scale

        def scale_point(point):
            return scale_value(point[0]), scale_value(point[1])

        def append_entity(entity: Dict):
            if "bbox" not in entity or entity["bbox"] is None:
                entity["bbox"] = cls._bbox_from_points(entity.get("points", []))
            entities.append(entity)

        def track_points(points):
            for x, y in points:
                all_points.append((float(x), float(y)))

        def add_line(start, end):
            points = [
                scale_point(start),
                scale_point(end),
            ]
            track_points(points)
            append_entity(
                {
                    "type": "LINE",
                    "start": points[0],
                    "end": points[1],
                    "bbox": cls._bbox_from_points(points),
                }
            )

        def add_polyline(points, closed=False):
            if len(points) < 2:
                return
            clean = [scale_point((x, y)) for x, y in points]
            track_points(clean)
            append_entity(
                {
                    "type": "POLYLINE",
                    "points": clean,
                    "closed": bool(closed),
                    "bbox": cls._bbox_from_points(clean),
                }
            )

        def add_text_entity(insert, text, height=2.5, rotation=0.0):
            x, y = scale_point(insert)
            h = scale_value(height or 2.5)
            track_points([(x, y), (x + h, y + h)])
            append_entity(
                {
                    "type": "TEXT",
                    "insert": (x, y),
                    "text": str(text),
                    "height": h,
                    "rotation": float(rotation or 0.0),
                    "bbox": (x, y - h, x + max(h, len(str(text)) * h * 0.6), y + h),
                }
            )

        def add_circle(center, radius):
            cx, cy = scale_point(center)
            r = scale_value(radius)
            bbox = (cx - r, cy - r, cx + r, cy + r)
            track_points([(bbox[0], bbox[1]), (bbox[2], bbox[3])])
            append_entity(
                {"type": "CIRCLE", "center": (cx, cy), "radius": r, "bbox": bbox}
            )

        def add_arc(center, radius, start_angle, end_angle):
            cx, cy = scale_point(center)
            r = scale_value(radius)
            bbox = (cx - r, cy - r, cx + r, cy + r)
            track_points([(bbox[0], bbox[1]), (bbox[2], bbox[3])])
            append_entity(
                {
                    "type": "ARC",
                    "center": (cx, cy),
                    "radius": r,
                    "start_angle": float(start_angle),
                    "end_angle": float(end_angle),
                    "bbox": bbox,
                }
            )

        def load_hatch(entity):
            try:
                boundary_paths = entity.paths
            except Exception:
                return

            for path in boundary_paths:
                points = []
                try:
                    if hasattr(path, "vertices"):
                        for vx in path.vertices:
                            points.append((float(vx[0]), float(vx[1])))
                    elif hasattr(path, "edges"):
                        for edge in path.edges:
                            edge_type = edge.__class__.__name__
                            if edge_type == "LineEdge":
                                points.append(
                                    (float(edge.start[0]), float(edge.start[1]))
                                )
                                points.append((float(edge.end[0]), float(edge.end[1])))
                            elif edge_type == "ArcEdge":
                                cx = float(edge.center[0])
                                cy = float(edge.center[1])
                                r = float(edge.radius)
                                start = math.radians(float(edge.start_angle))
                                end = math.radians(float(edge.end_angle))
                                if end < start:
                                    end += math.tau
                                steps = 24
                                for i in range(steps + 1):
                                    a = start + ((end - start) * i / steps)
                                    points.append(
                                        (cx + (r * math.cos(a)), cy + (r * math.sin(a)))
                                    )
                    if points:
                        add_polyline(points, closed=True)
                except Exception:
                    continue

        def load_insert(entity, doc_ref):
            try:
                block = doc_ref.blocks.get(entity.dxf.name)
            except Exception:
                return

            insert = entity.dxf.insert
            ix = float(insert.x)
            iy = float(insert.y)
            sx = float(getattr(entity.dxf, "xscale", 1.0) or 1.0)
            sy = float(getattr(entity.dxf, "yscale", 1.0) or 1.0)
            rotation = math.radians(float(getattr(entity.dxf, "rotation", 0.0) or 0.0))
            cos_r = math.cos(rotation)
            sin_r = math.sin(rotation)

            def transform_point(x, y):
                x *= sx
                y *= sy
                rx = (x * cos_r) - (y * sin_r)
                ry = (x * sin_r) + (y * cos_r)
                return ix + rx, iy + ry

            for child in block:
                try:
                    dtype = child.dxftype()
                    if dtype == "LINE":
                        s = child.dxf.start
                        e = child.dxf.end
                        add_line(transform_point(s.x, s.y), transform_point(e.x, e.y))
                    elif dtype in {"LWPOLYLINE", "POLYLINE"}:
                        points = []
                        try:
                            raw_points = list(child.get_points())
                            for p in raw_points:
                                points.append(transform_point(float(p[0]), float(p[1])))
                        except Exception:
                            try:
                                for v in child.vertices:
                                    points.append(
                                        transform_point(
                                            float(v.dxf.location.x),
                                            float(v.dxf.location.y),
                                        )
                                    )
                            except Exception:
                                continue
                        add_polyline(
                            points, closed=bool(getattr(child, "closed", False))
                        )
                    elif dtype == "TEXT":
                        p = child.dxf.insert
                        tx, ty = transform_point(p.x, p.y)
                        add_text_entity(
                            (tx, ty),
                            child.dxf.text,
                            child.dxf.height,
                            float(getattr(child.dxf, "rotation", 0.0) or 0.0),
                        )
                    elif dtype == "MTEXT":
                        p = child.dxf.insert
                        tx, ty = transform_point(p.x, p.y)
                        add_text_entity(
                            (tx, ty),
                            child.text,
                            child.dxf.char_height,
                            float(getattr(child.dxf, "rotation", 0.0) or 0.0),
                        )
                except Exception:
                    continue

        for entity in msp:
            dtype = entity.dxftype()
            if dtype == "LINE":
                start = entity.dxf.start
                end = entity.dxf.end
                add_line((start.x, start.y), (end.x, end.y))
            elif dtype in {"LWPOLYLINE", "POLYLINE"}:
                points = []
                try:
                    raw_points = list(entity.get_points())
                    for p in raw_points:
                        points.append((float(p[0]), float(p[1])))
                except Exception:
                    try:
                        for v in entity.vertices:
                            points.append(
                                (float(v.dxf.location.x), float(v.dxf.location.y))
                            )
                    except Exception:
                        continue
                add_polyline(points, closed=bool(getattr(entity, "closed", False)))
            elif dtype == "CIRCLE":
                center = entity.dxf.center
                add_circle((center.x, center.y), entity.dxf.radius)
            elif dtype == "ARC":
                center = entity.dxf.center
                add_arc(
                    (center.x, center.y),
                    entity.dxf.radius,
                    entity.dxf.start_angle,
                    entity.dxf.end_angle,
                )
            elif dtype == "TEXT":
                insert = entity.dxf.insert
                add_text_entity(
                    (insert.x, insert.y),
                    entity.dxf.text,
                    entity.dxf.height,
                    getattr(entity.dxf, "rotation", 0.0),
                )
            elif dtype == "MTEXT":
                insert = entity.dxf.insert
                add_text_entity(
                    (insert.x, insert.y),
                    entity.text,
                    entity.dxf.char_height,
                    getattr(entity.dxf, "rotation", 0.0),
                )
            elif dtype == "HATCH":
                load_hatch(entity)
            elif dtype == "INSERT":
                load_insert(entity, doc)

        bounds = (
            cls._bbox_from_points(all_points)
            if all_points
            else (0.0, 0.0, 100.0, 100.0)
        )
        return {"path": path, "entities": entities, "bounds": bounds}

    def load(self, path: str):
        payload = self.load_content(path)
        self.set_content(payload["path"], payload["entities"], payload["bounds"])

    def scene_rect(self, padding: float = 40.0) -> QRectF:
        if not self.bounds:
            return QRectF(-padding, -padding, 100 + 2 * padding, 100 + 2 * padding)
        min_x, min_y, max_x, max_y = self.bounds
        return QRectF(
            min_x - padding,
            -(max_y + padding),
            (max_x - min_x) + 2 * padding,
            (max_y - min_y) + 2 * padding,
        )

    def populate_graphics_scene(self, scene: QGraphicsScene, view_scale: float = 1.0):
        pen_line = QPen(QColor("#858585"))
        pen_line.setWidthF(0.0)

        pen_poly = QPen(QColor("#bebebe"))
        pen_poly.setWidthF(0.0)

        pen_arc = QPen(QColor("#2e2e2e"))
        pen_arc.setWidthF(0.0)

        line_path = QPainterPath()
        poly_path = QPainterPath()
        arc_path = QPainterPath()
        text_items = []
        created_items = []

        for entity in self.entities:
            etype = entity["type"]

            if etype == "LINE":
                x1, y1 = entity["start"]
                x2, y2 = entity["end"]
                line_path.moveTo(x1, -y1)
                line_path.lineTo(x2, -y2)

            elif etype == "POLYLINE":
                pts = [QPointF(x, -y) for x, y in entity["points"]]
                if len(pts) >= 2:
                    poly_path.moveTo(pts[0])
                    for pt in pts[1:]:
                        poly_path.lineTo(pt)
                    if entity.get("closed"):
                        poly_path.closeSubpath()

            elif etype == "CIRCLE":
                cx, cy = entity["center"]
                r = float(entity["radius"])
                arc_path.addEllipse(QRectF(cx - r, -(cy + r), r * 2, r * 2))

            elif etype == "ARC":
                cx, cy = entity["center"]
                r = float(entity["radius"])
                start_angle = float(entity.get("start_angle", 0.0))
                end_angle = float(entity.get("end_angle", 0.0))
                span_angle = end_angle - start_angle
                if span_angle <= 0:
                    span_angle += 360.0
                rect = QRectF(cx - r, -(cy + r), r * 2, r * 2)
                arc_path.arcMoveTo(rect, -start_angle)
                arc_path.arcTo(rect, -start_angle, -span_angle)

            elif etype == "TEXT":
                text = (entity.get("text") or "").strip()
                if not text:
                    continue

                # Keep DXF labels as fixed-size scene objects.  They should not
                # ignore the view transform and should not change font pixel size
                # as the camera zooms.  Zooming in/out now scales them naturally
                # with the drawing geometry.
                text_height = float(entity.get("height") or 0.0)
                if text_height > 40.0:
                    continue

                x, y = entity["insert"]
                item = QGraphicsSimpleTextItem(text)
                item.setBrush(QBrush(QColor("#C0C0C0")))

                font = QFont("Arial")
                font.setPointSizeF(10.0)
                font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
                font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
                item.setFont(font)

                bounds = item.boundingRect()
                if bounds.height() > 0:
                    # Match the visualiser's fixed scene-unit text behaviour.
                    item.setScale(0.45 / bounds.height())

                item.setPos(x, -y)
                item.setRotation(-float(entity.get("rotation", 0.0)))
                item.setCacheMode(QGraphicsItem.CacheMode.NoCache)
                text_items.append(item)

        if not line_path.isEmpty():
            item = QGraphicsPathItem(line_path)
            item.setPen(pen_line)
            item.setBrush(Qt.BrushStyle.NoBrush)
            item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
            scene.addItem(item)
            created_items.append(item)

        if not poly_path.isEmpty():
            item = QGraphicsPathItem(poly_path)
            item.setPen(pen_poly)
            item.setBrush(Qt.BrushStyle.NoBrush)
            item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
            scene.addItem(item)
            created_items.append(item)

        if not arc_path.isEmpty():
            item = QGraphicsPathItem(arc_path)
            item.setPen(pen_arc)
            item.setBrush(Qt.BrushStyle.NoBrush)
            item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
            scene.addItem(item)
            created_items.append(item)

        for item in text_items:
            scene.addItem(item)
            created_items.append(item)

        return created_items
