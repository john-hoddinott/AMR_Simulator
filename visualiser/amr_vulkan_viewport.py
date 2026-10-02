"""Experimental Vulkan viewport for the AMR visualiser.

This module deliberately keeps Qt widgets/dialogs in the normal PySide6 GUI
process and moves the map viewport to a QVulkanWindow when the local PySide6
build exposes Vulkan support.

Important: QVulkanWindow provides the swapchain and frame lifecycle, but actual
Vulkan draw calls require a Vulkan Python binding such as `vulkan`/`vulkan-sdk`
or a small native renderer.  The class below is a prepared integration point so
that the main visualiser can switch to a Vulkan window without changing the rest
of the UI.  If the Vulkan binding is not installed, it falls back cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

try:  # PySide6 exposes these only when Qt was built with Vulkan enabled.
    from PySide6.QtGui import QVulkanInstance, QVulkanWindow, QVulkanWindowRenderer
except Exception:  # pragma: no cover - platform/build dependent
    QVulkanInstance = None
    QVulkanWindow = None
    QVulkanWindowRenderer = object

try:  # Optional low-level Vulkan binding used by the renderer implementation.
    import vulkan as vk  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    vk = None


@dataclass
class VulkanDrawBatch:
    """Plain-data draw batch passed from the visualiser to the Vulkan viewport."""

    floor: int = 0
    line_vertices: List[Tuple[float, float, float, float, float, float]] = field(default_factory=list)
    rects: List[Dict[str, Any]] = field(default_factory=list)
    polygons: List[Dict[str, Any]] = field(default_factory=list)
    labels: List[Dict[str, Any]] = field(default_factory=list)
    viewport: Tuple[float, float, float, float] = (-1.0, -1.0, 1.0, 1.0)


class VulkanViewportRenderer(QVulkanWindowRenderer):
    """QVulkanWindow renderer hook.

    The window/swapchain lifecycle is real Vulkan.  Geometry upload/draw calls
    are intentionally isolated here so they can be moved to a native renderer
    later without touching the visualiser UI code.
    """

    def __init__(self, window: "VulkanViewportWindow"):
        super().__init__()
        self.window = window
        self.batch = VulkanDrawBatch()
        self.ready = False
        self.status = "Initialising Vulkan renderer"

    def initResources(self):  # Qt calls this when Vulkan resources are available.
        self.ready = True
        if vk is None:
            self.status = "Vulkan swapchain active; Python vulkan binding not installed, using clear-only frame."
        else:
            self.status = "Vulkan swapchain active; renderer ready for GPU draw batches."

    def initSwapChainResources(self):
        self.ready = True

    def releaseSwapChainResources(self):
        pass

    def releaseResources(self):
        self.ready = False

    def set_batch(self, batch: VulkanDrawBatch):
        self.batch = batch
        try:
            self.window.requestUpdate()
        except Exception:
            pass

    def startNextFrame(self):
        """Present the next Vulkan frame.

        A full production implementation binds pipelines and draws the batch's
        vertex buffers here.  When the low-level binding is absent we still let
        Qt present a valid frame so the application can run rather than crash.
        """
        # QVulkanWindow owns the command buffer.  PySide exposes the frame
        # lifecycle but not every Vulkan helper uniformly across versions.  Keep
        # this method conservative and always present the frame.
        try:
            self.window.frameReady()
        except Exception:
            pass


class VulkanViewportWindow(QVulkanWindow if QVulkanWindow is not None else QWindow):
    renderer_created = Signal(object)

    def __init__(self):
        super().__init__()
        self.renderer: Optional[VulkanViewportRenderer] = None
        try:
            self.setTitle("AMR Vulkan viewport")
        except Exception:
            pass

    def createRenderer(self):
        self.renderer = VulkanViewportRenderer(self)
        self.renderer_created.emit(self.renderer)
        return self.renderer


class VulkanViewportWidget(QWidget):
    """QWidget wrapper used by the main visualiser layout."""

    backend_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.renderer: Optional[VulkanViewportRenderer] = None
        self.vulkan_window = None
        self.window_container = None
        self.fallback_label = None
        self.available = False
        self.status = "Vulkan not initialised"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if QVulkanInstance is None or QVulkanWindow is None:
            self.status = "This PySide6/Qt build does not expose QVulkanWindow."
            self._show_fallback(layout)
            return

        instance = QVulkanInstance()
        if not instance.create():
            self.status = "Failed to create Vulkan instance. Check Vulkan runtime/GPU driver."
            self._show_fallback(layout)
            return

        self.vulkan_window = VulkanViewportWindow()
        self.vulkan_window.setVulkanInstance(instance)
        self.vulkan_window.renderer_created.connect(self._on_renderer_created)
        self.window_container = QWidget.createWindowContainer(self.vulkan_window, self)
        self.window_container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.window_container, 1)
        self.available = True
        self.status = "Vulkan viewport active"
        self.backend_changed.emit("vulkan")

    def _show_fallback(self, layout: QVBoxLayout):
        self.fallback_label = QLabel(self.status)
        self.fallback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fallback_label.setStyleSheet("background:#111;color:#ddd;padding:24px;")
        layout.addWidget(self.fallback_label, 1)
        self.backend_changed.emit("fallback")

    def _on_renderer_created(self, renderer):
        self.renderer = renderer

    def set_draw_batch(self, batch: VulkanDrawBatch):
        if self.renderer is not None:
            self.renderer.set_batch(batch)

    def current_status(self) -> str:
        if self.renderer is not None:
            return self.renderer.status
        return self.status
