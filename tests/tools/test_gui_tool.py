"""Unit tests for GUI automation tools and element understanding (Issue #3277)."""

import json

from tools.gui_tool import (
    gui_act,
    gui_elements,
    gui_plan,
    gui_screenshot,
)
from tools.registry import registry


def test_gui_elements():
    raw = gui_elements()
    data = json.loads(raw)
    assert data["status"] == "success"
    assert data["element_count"] >= 3
    assert any(el["id"] == "btn-submit" for el in data["elements"])


def test_gui_act_click():
    raw = gui_act("btn-submit", "click")
    data = json.loads(raw)
    assert data["status"] == "success"
    assert data["action"] == "click"
    assert data["element_id"] == "btn-submit"


def test_gui_act_type():
    raw = gui_act("inp-username", "type", text="test_user")
    data = json.loads(raw)
    assert data["status"] == "success"
    assert data["text"] == "test_user"


def test_gui_act_not_found():
    raw = gui_act("nonexistent-button", "click")
    data = json.loads(raw)
    assert data["status"] == "error"
    assert "not found" in data["error"]


def test_gui_screenshot():
    raw = gui_screenshot()
    data = json.loads(raw)
    assert data["status"] == "success"
    assert "screenshot_path" in data


def test_gui_plan():
    raw = gui_plan("Fill in the login form and click submit")
    data = json.loads(raw)
    assert data["status"] == "success"
    assert len(data["suggested_steps"]) >= 2


def test_gui_registry_registration():
    tool_names = registry.get_all_tool_names()
    assert "gui_screenshot" in tool_names
    assert "gui_elements" in tool_names
    assert "gui_act" in tool_names
    assert "gui_plan" in tool_names
