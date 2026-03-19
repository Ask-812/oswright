"""
Windows UI Automation backend - deterministic element finding via accessibility tree.

This is the equivalent of Playwright's accessibility snapshot. Instead of OCR
(probabilistic), this uses the Windows UI Automation API to find elements by
their actual role and name — 100% accurate, instant, no model needed.

Used automatically on Windows when available. Falls back to OCR when UIA
can't find elements (e.g., in apps without proper accessibility support).
"""

import logging
import platform
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()
_UIA_AVAILABLE = False

if _SYSTEM == "Windows":
    try:
        import uiautomation as auto
        _UIA_AVAILABLE = True
    except ImportError:
        pass


def is_available() -> bool:
    """Check if UI Automation is available."""
    return _UIA_AVAILABLE


@dataclass
class UIElement:
    """A UI element found via the accessibility tree."""
    name: str
    control_type: str
    automation_id: str
    class_name: str
    x: int
    y: int
    left: int
    top: int
    width: int
    height: int
    is_enabled: bool
    is_offscreen: bool
    value: Optional[str] = None
    children_count: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return (self.x, self.y)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "x": self.x,
            "y": self.y,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "is_enabled": self.is_enabled,
            "value": self.value,
        }


def _control_to_element(ctrl) -> Optional[UIElement]:
    """Convert a UIA control to a UIElement."""
    try:
        rect = ctrl.BoundingRectangle
        if rect.width() <= 0 or rect.height() <= 0:
            return None

        value = None
        try:
            vp = ctrl.GetValuePattern()
            if vp:
                value = vp.Value
        except Exception:
            pass

        return UIElement(
            name=ctrl.Name or "",
            control_type=ctrl.ControlTypeName or "",
            automation_id=ctrl.AutomationId or "",
            class_name=ctrl.ClassName or "",
            x=int(rect.left + rect.width() / 2),
            y=int(rect.top + rect.height() / 2),
            left=int(rect.left),
            top=int(rect.top),
            width=int(rect.width()),
            height=int(rect.height()),
            is_enabled=ctrl.IsEnabled,
            is_offscreen=ctrl.IsOffscreen,
            value=value,
        )
    except Exception:
        return None


def get_focused_window_tree(max_depth: int = 5) -> list[UIElement]:
    """
    Get the accessibility tree of the currently focused window.
    Returns a flat list of all interactive UI elements.
    """
    if not _UIA_AVAILABLE:
        return []

    elements = []
    try:
        focused = auto.GetForegroundControl()
        if focused is None:
            return []
        _walk_tree(focused, elements, depth=0, max_depth=max_depth)
    except Exception as e:
        logger.warning("UIA tree walk failed: %s", e)

    return elements


def _walk_tree(ctrl, elements: list, depth: int, max_depth: int):
    """Recursively walk the UI tree and collect elements."""
    if depth > max_depth:
        return

    el = _control_to_element(ctrl)
    if el and not el.is_offscreen and el.name:
        elements.append(el)

    try:
        children = ctrl.GetChildren()
        for child in children:
            _walk_tree(child, elements, depth + 1, max_depth)
    except Exception:
        pass


def find_element(
    name: Optional[str] = None,
    control_type: Optional[str] = None,
    automation_id: Optional[str] = None,
    window_title: Optional[str] = None,
) -> Optional[UIElement]:
    """
    Find a single UI element by name, type, or automation ID.

    Args:
        name: Element name/label (substring match).
        control_type: Control type (Button, Edit, CheckBox, etc.).
        automation_id: Automation ID (exact match).
        window_title: Search within a specific window (substring match).

    Returns:
        The first matching UIElement, or None.
    """
    if not _UIA_AVAILABLE:
        return None

    try:
        if window_title:
            target = auto.WindowControl(searchDepth=1, SubName=window_title)
        else:
            target = auto.GetForegroundControl()

        if target is None:
            return None

        # Build search conditions
        conditions = {}
        if name:
            conditions["SubName"] = name
        if control_type:
            conditions["ControlType"] = getattr(
                auto.ControlType, control_type + "Control",
                getattr(auto.ControlType, control_type, None)
            )
        if automation_id:
            conditions["AutomationId"] = automation_id

        if not conditions:
            return None

        ctrl = target.Control(searchDepth=10, **conditions)
        if ctrl and ctrl.Exists(maxSearchSeconds=2):
            return _control_to_element(ctrl)
    except Exception as e:
        logger.debug("UIA find_element failed: %s", e)

    return None


def find_all_elements(
    name: Optional[str] = None,
    control_type: Optional[str] = None,
    window_title: Optional[str] = None,
    max_depth: int = 5,
) -> list[UIElement]:
    """
    Find all UI elements matching the criteria.

    Args:
        name: Element name/label (substring match, case-insensitive).
        control_type: Control type filter (Button, Edit, CheckBox, etc.).
        window_title: Search within a specific window.
        max_depth: Maximum tree depth to search.

    Returns:
        List of matching UIElements.
    """
    if not _UIA_AVAILABLE:
        return []

    try:
        if window_title:
            target = auto.WindowControl(searchDepth=1, SubName=window_title)
        else:
            target = auto.GetForegroundControl()

        if target is None:
            return []

        all_elements = []
        _walk_tree(target, all_elements, depth=0, max_depth=max_depth)

        # Filter
        results = []
        for el in all_elements:
            if name and name.lower() not in el.name.lower():
                continue
            if control_type and control_type.lower() != el.control_type.lower().replace("control", ""):
                continue
            results.append(el)

        return results
    except Exception as e:
        logger.debug("UIA find_all_elements failed: %s", e)
        return []


def click_element(
    name: Optional[str] = None,
    control_type: Optional[str] = None,
    automation_id: Optional[str] = None,
    window_title: Optional[str] = None,
) -> Optional[UIElement]:
    """
    Find a UI element and click it using the UIA Invoke pattern.
    Falls back to coordinate click if Invoke isn't supported.

    Returns the clicked element, or None if not found.
    """
    if not _UIA_AVAILABLE:
        return None

    try:
        if window_title:
            target = auto.WindowControl(searchDepth=1, SubName=window_title)
        else:
            target = auto.GetForegroundControl()

        if target is None:
            return None

        conditions = {}
        if name:
            conditions["SubName"] = name
        if control_type:
            conditions["ControlType"] = getattr(
                auto.ControlType, control_type + "Control",
                getattr(auto.ControlType, control_type, None)
            )
        if automation_id:
            conditions["AutomationId"] = automation_id

        if not conditions:
            return None

        ctrl = target.Control(searchDepth=10, **conditions)
        if ctrl and ctrl.Exists(maxSearchSeconds=2):
            # Try invoke pattern first (reliable)
            try:
                ip = ctrl.GetInvokePattern()
                if ip:
                    ip.Invoke()
                    return _control_to_element(ctrl)
            except Exception:
                pass

            # Fall back to click at center
            ctrl.Click()
            return _control_to_element(ctrl)
    except Exception as e:
        logger.debug("UIA click_element failed: %s", e)

    return None


def get_element_value(
    name: Optional[str] = None,
    automation_id: Optional[str] = None,
    window_title: Optional[str] = None,
) -> Optional[str]:
    """Get the value of a UI element (e.g., text in an edit box)."""
    if not _UIA_AVAILABLE:
        return None

    el = find_element(name=name, automation_id=automation_id, window_title=window_title)
    return el.value if el else None


def set_element_value(
    value: str,
    name: Optional[str] = None,
    automation_id: Optional[str] = None,
    window_title: Optional[str] = None,
) -> bool:
    """Set the value of a UI element (e.g., type into an edit box)."""
    if not _UIA_AVAILABLE:
        return False

    try:
        if window_title:
            target = auto.WindowControl(searchDepth=1, SubName=window_title)
        else:
            target = auto.GetForegroundControl()

        if target is None:
            return False

        conditions = {}
        if name:
            conditions["SubName"] = name
        if automation_id:
            conditions["AutomationId"] = automation_id

        if not conditions:
            return False

        ctrl = target.Control(searchDepth=10, **conditions)
        if ctrl and ctrl.Exists(maxSearchSeconds=2):
            try:
                vp = ctrl.GetValuePattern()
                if vp:
                    vp.SetValue(value)
                    return True
            except Exception:
                pass

            # Fallback: click and type
            ctrl.Click()
            import time
            time.sleep(0.05)
            from oswright.input import Keyboard
            Keyboard.press("Ctrl+A")
            time.sleep(0.02)
            Keyboard.type_text(value)
            return True
    except Exception as e:
        logger.debug("UIA set_element_value failed: %s", e)

    return False
